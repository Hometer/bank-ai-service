"""Verify public knowledge and dialogue state across real isolated server restarts."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="public-knowledge-restart-") as temporary:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        env = dict(os.environ, MODEL_MODE="demo", PUBLIC_KNOWLEDGE_ENABLED="1",
                   DATABASE_URL=f"sqlite:///{Path(temporary) / 'restart.sqlite3'}")

        def stop(process: subprocess.Popen[Any]) -> None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        def start() -> subprocess.Popen[Any]:
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
                    health = httpx.get(address + "/api/v1/health", timeout=1, trust_env=False)
                    if health.status_code == 200:
                        assert health.json()["knowledge_base_status"] == "available"
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            stop(process)
            raise RuntimeError("Verification server startup timed out")

        process = start()
        try:
            with httpx.Client(base_url=address, trust_env=False, timeout=5) as client:
                def read(path: str) -> dict[str, Any]:
                    response = client.get(path)
                    response.raise_for_status()
                    return response.json()

                def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
                    response = client.post(path, json=payload)
                    response.raise_for_status()
                    return response.json()

                sid = post("/api/v1/sessions", {"customer_type": "personal"})["id"]
                message_path = f"/api/v1/sessions/{sid}/messages"
                first = post(message_path, {"text": "个人自雇申请DreamCash贷款需要什么材料"})
                assert first["answer"]["sources"][0]["article_ids"] == ["BCHK-P11"]
                enterprise = post("/api/v1/sessions", {"customer_type": "enterprise"})["id"]
                services = f"/api/v1/sessions/{enterprise}/services"
                navigation = f"/api/v1/sessions/{enterprise}/navigation"
                view = read(services)
                assert len(view["catalog"]) == 6
                target = next(item for item in view["catalog"] if item["capability_id"] == "enterprise_opening")
                view = post(navigation, {"action": "start", "expected_revision": view["session_revision"],
                    "capability_id": target["capability_id"], "knowledge_version": target["knowledge_version"]})
                view = post(navigation, {"action": "next", "expected_revision": view["session_revision"]})
                assert view["navigation"]["step_index"] == 1
                stop(process)
                process = start()
                history = read(f"/api/v1/sessions/{sid}")
                assert history["messages"][-1]["answer"]["sources"] == first["answer"]["sources"]
                second = post(message_path, {"text": "那需要什么材料"})
                assert second["answer"]["sources"][0]["article_ids"] == ["BCHK-P11"]
                assert second["answer"]["processing"] == "knowledge"
                view = read(services)
                assert view["navigation"]["step_index"] == 1
                assert view["cards"] and all(card["valid"] for card in view["cards"])
                assert view["cards"][0]["source"]["references"]
                post(navigation, {"action": "cancel", "expected_revision": view["session_revision"]})
                stop(process)
                process = start()
                view = read(services)
                assert view["navigation"]["status"] == "cancelled" and not view["cards"]
                assert len(read(f"/api/v1/sessions/{sid}")["messages"]) == 4
                print("PASS: two real restarts restored sourced answers, self-employed context, "
                      "enterprise reading steps and cancellation; no model requests")
        finally:
            stop(process)


if __name__ == "__main__":
    main()
