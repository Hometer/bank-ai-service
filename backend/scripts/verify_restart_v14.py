"""Three real restarts of V1.4 state using isolated data and no paid calls."""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bank-v14-restart-") as temporary:
        database = Path(temporary) / "restart.sqlite3"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, MODEL_MODE="demo", DATABASE_URL=f"sqlite:///{database}")
        address = f"http://127.0.0.1:{port}"

        def stop(process: subprocess.Popen[bytes]) -> None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        def start() -> subprocess.Popen[bytes]:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Independent verification service failed")
                try:
                    if httpx.get(address+"/api/v1/health", trust_env=False, timeout=1).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            stop(process)
            raise RuntimeError("Verification startup timed out")

        process = start()
        try:
            with httpx.Client(base_url=address, trust_env=False, timeout=5) as client:
                def new() -> str:
                    r = client.post('/api/v1/sessions', json={'customer_type':'personal'})
                    r.raise_for_status()
                    return str(r.json()['id'])

                def ask(sid: str, text: str) -> dict:
                    r = client.post(f'/api/v1/sessions/{sid}/messages', json={'text':text})
                    r.raise_for_status()
                    return r.json()

                def history(sid: str) -> dict:
                    r = client.get(f'/api/v1/sessions/{sid}')
                    r.raise_for_status()
                    return r.json()

                ambiguous, cancel, topic = new(), new(), new()
                expiry = history(ambiguous)['session']['expires_at']
                for sid in (ambiguous, cancel):
                    ask(sid, '这个怎么办')
                    result = ask(sid, '那具体呢')
                    assert result['consultation']['clarification_attempts'] == 2
                    assert result['consultation']['status'] == 'awaiting'
                ask(topic, '个人开户')
                stop(process)
                process = start()
                for sid in (ambiguous, cancel):
                    assert history(sid)['consultation']['clarification_attempts'] == 2
                    assert history(sid)['consultation']['status'] == 'awaiting'
                assert ask(topic, '会收多少钱')['answer']['focus'] == 'fees'
                assert ask(ambiguous, '还是这个')['consultation']['status'] == 'exhausted'
                assert ask(cancel, '取消')['consultation']['status'] == 'cancelled'
                stop(process)
                process = start()
                assert history(ambiguous)['consultation']['status'] == 'exhausted'
                assert history(cancel)['consultation']['status'] == 'cancelled'
                assert ask(ambiguous, '这个怎么办')['answer']['kind'] == 'unknown'
                assert history(ambiguous)['session']['expires_at'] == expiry
                assert history(topic)['consultation']['topic_ids'] == ['personal_opening']
                with sqlite3.connect(database) as db:
                    db.execute('UPDATE conversations SET expires_at=?', (time.time()-1,))
                stop(process)
                process = start()
                assert client.get(f'/api/v1/sessions/{ambiguous}').status_code == 404
                with sqlite3.connect(database) as db:
                    assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
                    assert db.execute('SELECT count(*) FROM conversations').fetchone()[0] == 0
                print('PASS: three actual restarts preserved awaiting(2), exhausted, cancellation and source-bound '
                      'topics; fixed expiry and cascade cleanup verified; no model requests.')
        finally:
            stop(process)


if __name__ == '__main__':
    main()
