import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import AnswerJob, Conversation, cleanup_expired
from app.main import create_app
from app.model_budget import BudgetUnavailable
from app.schemas import Answer, ConsultationContext, Source
from app.v15_answer import Evidence, Result, generate, parse_reply
from app.v15_budget import Ledger, initialize
from app.v15_web import PlainText, excerpt, safe_url

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'v15.db'}",
        v15_enabled=True,
        v15_budget_path=tmp_path / "budget.db",
        public_knowledge_path=ROOT / "docs/知识库/交通银行香港/knowledge-base.json",
        bank_faq_path=ROOT / "docs/知识库/香港银行客服/knowledge-base.json",
    )


@pytest.fixture
def evidence():
    return Evidence(
        id="S1",
        text="公开说明建议通过银行官方渠道核对办理条件。具体费用和时限需要查阅适用产品资料。",
        source=Source(id="k", title="公开说明", version="1"),
        origin="knowledge",
    )


def fake_result(*args):
    return Result(
        answer=Answer(
            kind="answered", text="真实测试替身，仅验证工程流程。", mode="model", processing="model"
        ),
        evidence=[],
        processing="deepseek",
        web_status="not_needed",
        intent=args[5],
        draft=args[5] == "agent_assist",
        context=ConsultationContext(),
    )


def session(client, kind="personal"):
    return client.post("/api/v1/sessions", json={"customer_type": kind}).json()["id"]


def submit(client, sid, text="交银香港转数快是什么", request="test-job-01", revision=0, intent="answer"):
    return client.post(
        f"/api/v1/sessions/{sid}/answer-jobs",
        json={"text": text, "expected_revision": revision, "request_id": request, "intent": intent},
    )


def wait(client, sid, jid):
    for _ in range(100):
        result = client.get(f"/api/v1/sessions/{sid}/answer-jobs/{jid}").json()
        if result["status"] not in ("pending", "running"):
            return result
        time.sleep(0.01)
    pytest.fail("Job did not terminate")


def test_valid_quotes(evidence):
    reply = parse_reply(
        json.dumps(
            {
                "claims": [{"source_id": "S1", "quote": evidence.text.split("。")[0] + "。"}],
                "unanswered": True,
            }
        ),
        [evidence],
    )
    assert reply.claims[0].source_id == "S1"


@pytest.mark.parametrize(
    "patch",
    [
        {
            "claims": [{"source_id": "fake", "quote": "公开说明建议通过银行官方渠道核对办理条件。"}],
            "unanswered": False,
        },
        {
            "claims": [{"source_id": "S1", "quote": "开户肯定成功且完全免费，已经办理所有相关业务。"}],
            "unanswered": False,
        },
        {"claims": [], "unanswered": False},
        {"claims": [], "unanswered": "false"},
        {"claims": [], "unanswered": True, "execute_transfer": True},
    ],
)
def test_invalid_generation(evidence, patch):
    with pytest.raises(ValueError):
        parse_reply(json.dumps(patch), [evidence])


@pytest.mark.parametrize(
    "url",
    [
        "http://www.hk.bankcomm.com/hk/",
        "https://127.0.0.1/",
        "https://example.com/",
        "https://www.hk.bankcomm.com:8000/hk/",
        "https://user:secret@www.hk.bankcomm.com/hk/",
        "https://www.hk.bankcomm.com/hk/#secret",
    ],
)
def test_web_url_boundaries(url):
    with pytest.raises(ValueError):
        safe_url(url, resolve=False)


def test_web_private_dns(monkeypatch):
    monkeypatch.setattr(
        "app.v15_web.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.1", 443))]
    )
    with pytest.raises(ValueError):
        safe_url("https://www.hk.bankcomm.com/hk/")


def test_plain_text_and_matching():
    parser = PlainText()
    parser.feed("<script>secret()</script><p>转数快是一项支付服务。</p><p>相关条件请查官网。</p>")
    text = "\n".join(parser.parts)
    assert "secret" not in text and "支付服务" in excerpt(text, ["转数快"])
    assert excerpt(text, ["mortgage"]) == ""


def test_refund_excerpt_precedes_generic_fps_overview():
    text = "轉數快概览\n这是与退款期限无关的普通菜单。\n功能目录\n企业服务\n其他内容\n更多菜单\n轉數快退款注意事項\n只可於收到款項後的30天內辦理及為一次性，不可重複提交。\n"
    value = excerpt(text, ["轉數快", "FPS", "退款"])
    assert "30天" in value[:400]


def test_quote_must_keep_negation_and_full_conditions(evidence):
    evidence.text = "不保证贷款一定获批，需要银行实际审批。"
    raw = json.dumps(
        {
            "claims": [{"source_id": "S1", "quote": "保证贷款一定获批，需要银行实际审批。"}],
            "unanswered": False,
        }
    )
    with pytest.raises(ValueError):
        parse_reply(raw, [evidence])


def test_budget_durable_no_reset(tmp_path):
    path = tmp_path / "budget.db"
    initialize(path)
    for _ in range(50):
        ledger = Ledger(path)
        ticket = ledger.reserve()
        ledger.finish(ticket, "validated", {"prompt_tokens": 10, "completion_tokens": 5})
    assert Ledger(path).snapshot()["requests"] == 50
    with pytest.raises(BudgetUnavailable):
        Ledger(path).reserve()
    with pytest.raises(FileExistsError):
        initialize(path)


def test_budget_exceeded_freezes(tmp_path):
    path = tmp_path / "budget.db"
    initialize(path)
    ledger = Ledger(path)
    ticket = ledger.reserve()
    with pytest.raises(BudgetUnavailable):
        ledger.finish(ticket, "validated", {"prompt_tokens": 7000, "completion_tokens": 1})
    with pytest.raises(BudgetUnavailable):
        ledger.reserve()


def test_job_history_restart_idempotency(settings, monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        return fake_result(*args)

    monkeypatch.setattr("app.v15_api.run_answer", run)
    app = create_app(settings)
    with TestClient(app) as client:
        sid = session(client)
        response = submit(client, sid)
        assert response.status_code == 202
        jid = response.json()["id"]
        done = wait(client, sid, jid)
        assert done["status"] == "completed" and done["revision"] == 2
        assert submit(client, sid).json()["id"] == jid and len(calls) == 1
        assert submit(client, sid, text="改了问题").status_code == 409
        assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2
        cookies = dict(client.cookies)
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get(f"/api/v1/sessions/{sid}/answer-jobs/{jid}").json()["status"] == "completed"
        assert len(restarted.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2
        assert len(calls) == 1


@pytest.mark.parametrize(
    "text", ["验证码是123456", "银行卡被盗刷了", "查余额", "转人工", " ", "我的账户状态"]
)
def test_protected_questions_never_call_model(settings, monkeypatch, text):
    monkeypatch.setattr(
        "app.v15_api.run_answer", lambda *a: pytest.fail("Protected route made external call")
    )
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        assert submit(client, sid, text=text).status_code == 422
        assert client.get(f"/api/v1/sessions/{sid}").json()["session"]["revision"] == 0


def test_owner_isolation(settings, monkeypatch):
    monkeypatch.setattr("app.v15_api.run_answer", fake_result)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = submit(client, sid).json()["id"]
        wait(client, sid, jid)
        client.cookies.clear()
        session(client, "enterprise")
        assert client.get(f"/api/v1/sessions/{sid}/answer-jobs/{jid}").status_code == 404
        assert client.post(f"/api/v1/sessions/{sid}/answer-jobs/{jid}/cancel").status_code == 404


def test_cancellation_discards_late_reply(settings, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def delayed(*args):
        entered.set()
        release.wait(2)
        return fake_result(*args)

    monkeypatch.setattr("app.v15_api.run_answer", delayed)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = submit(client, sid).json()["id"]
        assert entered.wait(2)
        assert client.post(f"/api/v1/sessions/{sid}/answer-jobs/{jid}/cancel").json()["status"] == "cancelled"
        release.set()
        time.sleep(0.05)
        assert wait(client, sid, jid)["status"] == "cancelled"
        assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_failures_end_job(settings, monkeypatch):
    def broken(*args):
        raise RuntimeError("do not expose stack")

    monkeypatch.setattr("app.v15_api.run_answer", broken)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = submit(client, sid).json()["id"]
        assert wait(client, sid, jid)["status"] == "failed"


def test_restart_interrupts_pending_without_model(settings, monkeypatch):
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        cookies = dict(client.cookies)
        with client.app.state.sessions.begin() as db:
            db.add(
                AnswerJob(
                    id="interrupted",
                    session_id=sid,
                    request_id="interrupted",
                    question="公开问题",
                    intent="answer",
                    revision=0,
                    status="running",
                    created_at=time.time(),
                )
            )
    monkeypatch.setattr(
        "app.v15_api.run_answer", lambda *a: pytest.fail("Restart must not repeat paid calls")
    )
    with TestClient(create_app(settings)) as client:
        client.cookies.update(cookies)
        assert client.get(f"/api/v1/sessions/{sid}/answer-jobs/interrupted").json()["status"] == "failed"


def test_jobs_expire_with_conversation(settings):
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        with client.app.state.sessions.begin() as db:
            db.add(
                AnswerJob(
                    id="expired",
                    session_id=sid,
                    request_id="expired",
                    question="公开问题",
                    intent="answer",
                    revision=0,
                    status="completed",
                    created_at=time.time(),
                )
            )
            db.get(Conversation, sid).expires_at = time.time() - 1
        cleanup_expired(client.app.state.engine)
        with client.app.state.sessions() as db:
            assert db.get(AnswerJob, "expired") is None


def test_disabled_keeps_original_api(settings):
    with TestClient(create_app(replace(settings, v15_enabled=False))) as client:
        assert client.get("/api/v1/capabilities").json()["generation"] is False
        assert submit(client, session(client)).status_code == 503
        assert client.get("/app.js").status_code == 200


@pytest.mark.parametrize("case", ["valid", "invalid", "rejected", "initialization", "cancelled", "input_cap"])
def test_real_contract_transport_failures_are_bounded(settings, evidence, monkeypatch, case):
    from threading import Event

    import httpx

    settings = replace(
        settings,
        model_name="deepseek-flash",
        model_base_url="https://api.deepseek.com",
        model_api_key="fake-test-placeholder",
    )
    initialize(settings.v15_budget_path)
    calls = []

    async def handler(request):
        calls.append(request)
        if case == "rejected":
            return httpx.Response(401, json={"error": {"message": "test rejected"}})
        content = (
            json.dumps(
                {
                    "claims": [{"source_id": "S1", "quote": evidence.text.split("。")[0] + "。"}],
                    "unanswered": False,
                }
            )
            if case == "valid"
            else '{"claims":[{"source_id":"invented","quote":"虚构不存在的条款，应被确定性代码拒绝。"}],"unanswered":false}'
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    async def construct(*args):
        if case == "initialization":
            raise RuntimeError("Client initialization failed")
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.v15_answer.construct_client", construct)
    cancelled = Event()
    if case == "cancelled":
        cancelled.set()
    question = "公开问题" if case != "input_cap" else "超长公开问题" * 2000
    if case == "valid":
        reply = asyncio.run(generate(question, "personal", [evidence], settings, cancelled))
        assert reply.claims
    else:
        with pytest.raises(Exception):
            asyncio.run(generate(question, "personal", [evidence], settings, cancelled))
    expected = {"valid": 1, "invalid": 2, "rejected": 1, "initialization": 1, "cancelled": 0, "input_cap": 0}[
        case
    ]
    assert Ledger(settings.v15_budget_path).snapshot()["requests"] == expected
    assert len(calls) < 3


def test_source_change_blocks_replay(settings, monkeypatch):
    from app.schemas import PublicReference
    from app.v15_answer import Evidence

    source = Source(
        id="changed",
        title="Withdrawn",
        version="1",
        references=[
            PublicReference(
                id="fake-source",
                title="Removed official source",
                url="https://www.hk.bankcomm.com/hk/removed",
                publisher="交银香港",
                entity="交银香港",
                retrieved_on="2026-10-04",
                locator="test",
            )
        ],
    )
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        result = fake_result(None, None, None, None, None, "answer")
        result.evidence = [
            Evidence(
                id="WEB-1",
                text="这是一条已经从可用来源中撤回的旧说明。",
                source=source,
                origin="official_web",
            )
        ]
        with client.app.state.sessions.begin() as db:
            db.add(
                AnswerJob(
                    id="changed",
                    session_id=sid,
                    request_id="changed-request",
                    question="公开问题",
                    intent="answer",
                    revision=0,
                    status="completed",
                    result=result.model_dump(),
                    created_at=time.time(),
                )
            )
        assert client.get(f"/api/v1/sessions/{sid}/answer-jobs/changed").status_code == 409


def test_speech_owner_bound_and_current_validated_turn_only(settings, monkeypatch, tmp_path):
    calls = []

    def local_speech(text):
        calls.append(text)
        directory = tmp_path / "speech"
        directory.mkdir()
        file = directory / "reply.wav"
        file.write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 60)
        return directory, file

    monkeypatch.setattr("app.v15_speech.synthesize", local_speech)
    monkeypatch.setattr("app.v15_api.run_answer", fake_result)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = submit(client, sid).json()["id"]
        wait(client, sid, jid)
        response = client.post(f"/api/v1/sessions/{sid}/answer-jobs/{jid}/speech")
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
        assert len(calls) == 1 and not (tmp_path / "speech").exists()
        client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "转人工"})
        assert client.post(f"/api/v1/sessions/{sid}/answer-jobs/{jid}/speech").status_code == 409
        assert len(calls) == 1


def test_local_speech_failure_does_not_break_text_history(settings, monkeypatch):
    def failed(*args):
        raise ValueError("No local voice")

    monkeypatch.setattr("app.v15_speech.synthesize", failed)
    monkeypatch.setattr("app.v15_api.run_answer", fake_result)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = submit(client, sid).json()["id"]
        wait(client, sid, jid)
        response = client.post(f"/api/v1/sessions/{sid}/answer-jobs/{jid}/speech")
        assert response.status_code == 503 and set(response.json()) == {"error"}
        assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2
