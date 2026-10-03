"""Approved ten-case V1.4 sequence. Same budget, private atomic journal, no blind resend."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.model_budget import BudgetLedger  # noqa: E402

DATA = ROOT / 'data'
JOURNAL = DATA / 'v14-model-smoke-journal.json'
REPORT = DATA / 'v14-model-smoke-report.json'
LEDGER = DATA / 'model-budget.sqlite3'
ADDRESS = 'http://127.0.0.1:18766'
CASES = [
    ('R14-01', 'A', 'personal', '想办一张用于日常收付款的借记卡，要先了解什么', 'answered', ['personal_opening'], ['general']),
    ('R14-02', 'A', 'personal', '会收多少钱', 'answered', ['personal_opening'], ['fees']),
    ('R14-03', 'A', 'personal', '不是开户，是定期没到期想取出来，应该看哪份规定', 'answered', ['personal_deposit'], ['general']),
    ('R14-04', 'B', 'personal', '借的钱想提前还，先要核对什么约定', 'answered', ['personal_repayment'], ['general']),
    ('R14-05', 'C', 'enterprise', '公司开户的办理要求先看什么说明', 'answered', ['enterprise_opening'], ['qualification']),
    ('R14-06', 'U', 'personal', '信用卡积分怎么兑换', 'unknown', [], []),
    ('R14-07', 'D', 'enterprise', '公司的融资肯定能批吗？企业回单现在能下载吗', 'answered',
     ['enterprise_financing', 'enterprise_reconciliation'], ['guarantee', 'download']),
    ('R14-08', 'E', 'personal', '这个怎么办', 'clarification', [], []),
    ('R14-09', 'E', 'personal', '个人开户', 'answered', ['personal_opening'], ['general']),
    ('R14-10', 'F', 'personal', '个人开户和企业开户各要了解什么', 'answered',
     ['personal_opening', 'enterprise_opening'], ['general', 'general']),
]


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.v14-validation-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start() -> subprocess.Popen[bytes]:
    env = dict(os.environ, MODEL_MODE='model', DATABASE_URL=f'sqlite:///{DATA / "v14-model.sqlite3"}',
               MODEL_BASE_URL='https://api.deepseek.com', MODEL_NAME='deepseek-flash',
               MODEL_BUDGET_PATH=str(LEDGER))
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend',
        '--host', '127.0.0.1', '--port', '18766', '--no-access-log'], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Verification port unavailable')
        try:
            r = httpx.get(ADDRESS+'/api/v1/health', timeout=1, trust_env=False)
            if r.status_code == 200 and r.json()['mode'] == 'model':
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    stop(process)
    raise RuntimeError('Verification startup timed out')


def main() -> None:
    budget = BudgetLedger(LEDGER).snapshot()
    journal: dict[str, Any] = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {'cookie':'', 'sessions':{}, 'cases':[]}
    if journal.get('completed'):
        print('Existing completed run retained; inspect the report without repeating requests.')
        return
    missing = len(CASES)-len(journal['cases'])
    if (budget['max_requests']-budget['requests'] < missing
            or any(case['state'] != 'complete' or not case['passed'] for case in journal['cases'])):
        raise RuntimeError('Budget insufficient or interrupted case requires read-only reconciliation')
    process = start()
    try:
        with httpx.Client(base_url=ADDRESS, trust_env=False, timeout=40) as client:
            if journal['cookie']:
                client.cookies.set('bank_demo_owner', journal['cookie'], domain='127.0.0.1', path='/')
            for ident, group, customer, question, expected, source_ids, focuses in CASES:
                if any(case['id'] == ident for case in journal['cases']):
                    continue
                if group not in journal['sessions']:
                    created = client.post('/api/v1/sessions', json={'customer_type':customer})
                    created.raise_for_status()
                    journal['sessions'][group] = created.json()['id']
                sid = journal['sessions'][group]
                history = client.get(f'/api/v1/sessions/{sid}')
                history.raise_for_status()
                revision = history.json()['session']['revision']
                before = BudgetLedger(LEDGER).snapshot()['requests']
                case = {'id':ident, 'question':question, 'customer_type':customer, 'session_id':sid,
                        'expected_revision':revision, 'budget_start':before, 'state':'prepared'}
                journal['cookie'] = client.cookies.get('bank_demo_owner')
                journal['cases'].append(case)
                save(JOURNAL, journal)
                started = time.monotonic()
                result = client.post(f'/api/v1/sessions/{sid}/messages', json={
                    'text':question, 'expected_revision':revision})
                result.raise_for_status()
                turn = result.json()
                attempts = BudgetLedger(LEDGER).snapshot()['attempts'][before:]
                answer = turn['answer']
                passed = (1 <= len(attempts) <= 2 and all(item['outcome'] == 'validated' for item in attempts)
                          and answer['processing'] == 'model' and answer['kind'] == expected
                          and [source['id'] for source in answer['sources']] == source_ids)
                if focuses:
                    passed = passed and [part['focus'] for part in answer['parts']] == focuses
                else:
                    passed = passed and all(item['knowledge_id'] is None for item in attempts)
                case.update(state='complete', passed=passed, answer=answer, consultation=turn['consultation'],
                            attempts=attempts, duration_ms=round((time.monotonic()-started)*1000))
                save(JOURNAL, journal)
                save(REPORT, {'date':'2026-10-03', 'contract':'consultation-route-v14', 'model':'deepseek-flash',
                    'cases':journal['cases'], 'budget':BudgetLedger(LEDGER).snapshot(), 'rating_status':'待产品经理评分'})
                print(f'{ident}: requests={len(attempts)} kind={answer["kind"]} passed={passed}', flush=True)
                if not passed:
                    raise RuntimeError('Failed case; stopped before launching more paid requests')
            # No model call is required to clarify the pointer to the two already confirmed topics.
            before = BudgetLedger(LEDGER).snapshot()
            sid = journal['sessions']['F']
            result = client.post(f'/api/v1/sessions/{sid}/messages', json={'text':'那还需要什么'})
            result.raise_for_status()
            turn = result.json()
            assert turn['answer']['processing'] == 'rule' and turn['answer']['kind'] == 'clarification'
            assert BudgetLedger(LEDGER).snapshot() == before
            report = json.loads(REPORT.read_text())
            report['N14-01'] = {'answer':turn['answer'], 'consultation':turn['consultation'], 'model_requests':0}
            # Store the no-cost checkpoint to avoid issuing it again on a resumed completed run.
            stop(process)
            process = start()
            for case in journal['cases']:
                history = client.get(f'/api/v1/sessions/{case["session_id"]}')
                history.raise_for_status()
                messages = history.json()['messages']
                found = any(message['role'] == 'user' and message['text'] == case['question']
                            and index+1 < len(messages) and messages[index+1]['answer'] == case['answer']
                            for index, message in enumerate(messages))
                assert found
            assert BudgetLedger(LEDGER).snapshot() == before
            assert client.get(f'/api/v1/sessions/{sid}').json()['consultation']['status'] == 'awaiting'
            report['actual_process_restart_recovery'] = True
            report['budget'] = before
            save(REPORT, report)
            journal['completed'] = True
            save(JOURNAL, journal)
            print('PASS: real answers/state restored after restart; same budget, no request-count reset.', flush=True)
    finally:
        stop(process)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'中止类型：{type(error).__name__}（不输出异常内容）')
        print('新版真实验证已停止：先核对本地断点记录与费用台账；未自动重发不确定的请求。')
        raise SystemExit(1) from None
