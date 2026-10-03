"""Verify real restarts, V1.2 state and V1.3 navigation in an isolated database."""

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
                def service_view(session_id: str) -> dict:
                    response = client.get(f"/api/v1/sessions/{session_id}/services")
                    response.raise_for_status()
                    return response.json()

                def navigate(session_id: str, action: str, view: dict, capability: dict | None = None) -> dict:
                    payload = {"action": action, "expected_revision": view["session_revision"]}
                    if capability is not None:
                        payload.update(capability_id=capability["capability_id"],
                                       knowledge_version=capability["knowledge_version"])
                    response = client.post(f"/api/v1/sessions/{session_id}/navigation", json=payload)
                    response.raise_for_status()
                    return response.json()

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
                navigation_session = client.post("/api/v1/sessions", json={"customer_type": "enterprise"})
                navigation_session.raise_for_status()
                navigation_sid = navigation_session.json()["id"]
                original_expiry = navigation_session.json()["expires_at"]
                navigation = service_view(navigation_sid)
                navigation = navigate(navigation_sid, "start", navigation, navigation["catalog"][0])
                navigation = navigate(navigation_sid, "next", navigation)
                assert navigation["navigation"]["step_index"] == 1
                stop(process)
                process = start()
                history = client.get(f"/api/v1/sessions/{sid}")
                history.raise_for_status()
                assert len(history.json()["messages"]) == 6
                navigation = service_view(navigation_sid)
                assert navigation["navigation"]["status"] == "reading"
                assert navigation["navigation"]["step_index"] == 1
                assert navigation["cards"] and all(card["valid"] for card in navigation["cards"])
                navigation_history = client.get(f"/api/v1/sessions/{navigation_sid}").json()
                assert navigation_history["session"]["expires_at"] == original_expiry
                assert len(navigation_history["messages"]) == 2
                navigation = navigate(navigation_sid, "cancel", navigation)
                assert navigation["navigation"]["status"] == "cancelled"
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
                navigation = service_view(navigation_sid)
                assert navigation["navigation"]["status"] == "cancelled"
                assert navigation["cards"] == [] and navigation["suggestions"] == []
                assert client.get(f"/api/v1/sessions/{navigation_sid}").json()["session"]["expires_at"] == original_expiry
                followup = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "那需要什么"})
                followup.raise_for_status()
                assert followup.json()["answer"]["sources"][0]["id"] == "personal_opening"
                client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "疑似诈骗"}).raise_for_status()
                with sqlite3.connect(database) as db:
                    db.execute("UPDATE conversations SET expires_at = ? WHERE id = ?", (time.time() - 1, sid))
                    db.execute("UPDATE conversations SET expires_at = ? WHERE id = ?", (time.time() - 1, navigation_sid))
                    db.execute("UPDATE safety_notices SET expires_at = ?", (time.time() - 1,))
                stop(process)
                process = start()
                assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
                assert client.get(f"/api/v1/sessions/{navigation_sid}").status_code == 404
                assert client.get(f"/api/v1/sessions/{navigation_sid}/services").status_code == 404
                assert client.post(f"/api/v1/sessions/{navigation_sid}/navigation", json={
                    "action": "cancel", "expected_revision": navigation["session_revision"],
                }).status_code == 404
                assert not client.get("/api/v1/safety").json()["active"]
                with sqlite3.connect(database) as db:
                    assert db.execute("SELECT count(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0] == 0
                    assert db.execute("SELECT count(*) FROM messages WHERE session_id = ?", (navigation_sid,)).fetchone()[0] == 0
                    assert db.execute("SELECT count(*) FROM conversations WHERE id = ?", (navigation_sid,)).fetchone()[0] == 0
                    assert db.execute("SELECT count(*) FROM safety_notices").fetchone()[0] == 0
                print("PASS: three real restarts restored 6 V1.2 messages/topic/active notice and V1.3 cards/step; "
                      "preserved cancellation/dismissal and fixed expiry; rejected expired actions and removed all derived state")
        finally:
            stop(process)


if __name__ == "__main__":
    main()
