"""Verify real process restarts, V1.2 state and expiry in an isolated database."""

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
    with tempfile.TemporaryDirectory(prefix="bank-demo-restart-") as temporary:
        database = Path(temporary) / "restart.sqlite3"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        env = dict(os.environ, MODEL_MODE="demo", DATABASE_URL=f"sqlite:///{database}")

        def start() -> subprocess.Popen:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                 "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Verification server did not start")
                try:
                    if httpx.get(address + "/api/v1/health", timeout=1, trust_env=False).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            stop(process)
            raise RuntimeError("Verification server startup timed out")

        def stop(process: subprocess.Popen) -> None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        process = start()
        try:
            with httpx.Client(base_url=address, trust_env=False, timeout=5) as client:
                session = client.post("/api/v1/sessions", json={"customer_type": "personal"})
                session.raise_for_status()
                sid = session.json()["id"]
                reply = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"})
                reply.raise_for_status()
                assert reply.json()["answer"]["sources"][0]["id"] == "personal_opening"
                urgent = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "银行卡被盗刷了"})
                urgent.raise_for_status()
                assert urgent.json()["safety"]["active"]
                client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"}).raise_for_status()
                stop(process)
                process = start()
                history = client.get(f"/api/v1/sessions/{sid}")
                history.raise_for_status()
                assert len(history.json()["messages"]) == 6
                followup = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "那需要什么"})
                followup.raise_for_status()
                assert followup.json()["answer"]["sources"][0]["id"] == "personal_opening"
                state = client.get("/api/v1/safety").json()
                assert state["active"]
                closed = client.post("/api/v1/safety/dismiss", json={"expected_revision": state["revision"]})
                closed.raise_for_status()
                assert not closed.json()["active"]
                stop(process)
                process = start()
                assert not client.get("/api/v1/safety").json()["active"]
                followup = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "那需要什么"})
                followup.raise_for_status()
                assert followup.json()["answer"]["sources"][0]["id"] == "personal_opening"
                client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "疑似诈骗"}).raise_for_status()
                with sqlite3.connect(database) as db:
                    db.execute("UPDATE conversations SET expires_at = ? WHERE id = ?", (time.time() - 1, sid))
                    db.execute("UPDATE safety_notices SET expires_at = ?", (time.time() - 1,))
                stop(process)
                process = start()
                assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
                assert not client.get("/api/v1/safety").json()["active"]
                with sqlite3.connect(database) as db:
                    assert db.execute("SELECT count(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0] == 0
                    assert db.execute("SELECT count(*) FROM safety_notices").fetchone()[0] == 0
                print("PASS: three real restarts restored 6 messages/topic/active notice, preserved explicit dismissal, and removed expired messages/notice")
        finally:
            stop(process)


if __name__ == "__main__":
    main()
