import io
import math
import subprocess
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Conversation, Message, TranscriptionJob, cleanup_expired
from app.local_asr import (
    ASRError,
    check_wave,
    parse_transcript,
    run_tool,
    temp_root,
    transcribe,
    validate_container,
)
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'asr.sqlite3'}", v15_enabled=True, asr_enabled=True)


def session(client):
    return client.post("/api/v1/sessions", json={"customer_type": "personal"}).json()["id"]


def upload(client, sid, data=b"RIFF0000WAVEdata", mime="audio/wav", revision=0):
    return client.post(f"/api/v1/sessions/{sid}/transcription-jobs", content=data,
                       headers={"Content-Type": mime, "X-Expected-Revision": str(revision)})


def wait(client, sid, jid):
    for _ in range(100):
        job = client.get(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.01)
    pytest.fail("ASR task did not finish")


@pytest.mark.parametrize("data,mime", [
    (b"<html>attack</html>", "audio/webm"), (b"RIFF0000WAVEdata", "audio/mp4"),
    (b"#EXTM3U file:///etc/passwd", "audio/wav"), (b"OggS", "text/plain"),
    (b"", "audio/wav"), (b"x" * (2 * 1024 * 1024 + 1), "audio/webm"),
])
def test_reject_untrusted_container(data, mime):
    with pytest.raises(ASRError):
        validate_container(data, mime)


@pytest.mark.parametrize("raw", [None, {}, {"transcription": "text"}, {"transcription": [{}]},
                                      {"transcription": [{"text": 2}]}, {"transcription": [{"text": ""}]},
                                      {"transcription": [{"text": "x" * 1001}]}])
def test_validate_model_output(raw):
    with pytest.raises(ASRError):
        parse_transcript(raw)


def test_parse_safe_transcription():
    assert parse_transcript({"transcription": [{"text": "网上银行"}, {"text": "收费吗？"}]}) == "网上银行收费吗？"


def make_wave(seconds, amplitude):
    import array
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(array.array("h", (int(amplitude * math.sin(i * 0.1))
                                          for i in range(int(seconds * 16000)))).tobytes())
    return buf.getvalue()


@pytest.mark.parametrize("seconds,amplitude,code", [(0.1, 8000, "AUDIO_DURATION"),
                                                  (21, 8000, "AUDIO_DURATION"), (1, 0, "NO_SPEECH")])
def test_duration_and_silence(seconds, amplitude, code, tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(make_wave(seconds, amplitude))
    with pytest.raises(ASRError, match=code):
        check_wave(path)


def test_missing_model(settings):
    with pytest.raises(ASRError, match="ASR_UNAVAILABLE"):
        transcribe(b"RIFF0000WAVEdata", "audio/wav", replace(settings, asr_model_path=Path('/missing')), threading.Event())


def test_timeout_kills_subprocess():
    import sys
    start = time.monotonic()
    with pytest.raises(ASRError, match="ASR_TIMEOUT"):
        run_tool([sys.executable, "-c", "import time; time.sleep(30)"], 0.25, threading.Event())
    assert time.monotonic() - start < 2


def test_cancel_does_not_start_process(monkeypatch):
    event = threading.Event()
    event.set()
    def forbidden(*args, **kwargs):
        pytest.fail("Cancelled task must not start a tool")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ASRError, match="CANCELLED"):
        run_tool(["unused"], 10, event)


def test_decode_failure_cleans_ephemeral_audio(settings, monkeypatch):
    monkeypatch.setattr("app.local_asr.available", lambda _: True)
    def failed(*args):
        raise ASRError("ASR_FAILED")
    monkeypatch.setattr("app.local_asr.run_tool", failed)
    with pytest.raises(ASRError, match="ASR_FAILED"):
        transcribe(b"RIFF0000WAVEdata", "audio/wav", settings, threading.Event())
    assert not list(temp_root(settings).glob("turn-*"))


def test_owner_revision_review_restart_and_expiry(settings, monkeypatch):
    calls = []
    monkeypatch.setattr("app.local_asr_api.available", lambda _: True)
    def adapter(*args):
        calls.append(True)
        return "网上银行服务收费吗？"
    monkeypatch.setattr("app.local_asr_api.transcribe", adapter)
    app = create_app(settings)
    with TestClient(app) as client:
        sid = session(client)
        response = upload(client, sid)
        assert response.status_code == 202
        jid = response.json()["id"]
        job = wait(client, sid, jid)
        assert job["text"] == "网上银行服务收费吗？" and not job["confirmed"]
        with TestClient(app) as outsider:
            assert outsider.get(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}").status_code == 401
            session(outsider)
            assert outsider.get(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}").status_code == 404
        assert upload(client, sid, revision=99).status_code == 409
        assert upload(client, sid, b"invalid").status_code == 415
        assert client.post(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}/confirm",
                           json={"text": "香港网上银行收费吗？"}).json()["confirmed"]
        cookie = client.cookies.get("bank_demo_owner")
        with app.state.sessions() as db:
            assert db.scalar(select(Message).where(Message.session_id == sid)) is None
    with TestClient(create_app(settings)) as restored:
        restored.cookies.set("bank_demo_owner", cookie)
        job = restored.get(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}").json()
        assert job["confirmed"] and job["text"] == "香港网上银行收费吗？"
        assert len(calls) == 1
        with restored.app.state.sessions.begin() as db:
            db.get(Conversation, sid).expires_at = 1
        cleanup_expired(restored.app.state.engine)
        with restored.app.state.sessions() as db:
            assert db.get(TranscriptionJob, jid) is None


def test_cancel_drops_late_transcript(settings, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    monkeypatch.setattr("app.local_asr_api.available", lambda _: True)
    def slow(*args):
        started.set()
        finish.wait(2)
        return "late result"
    monkeypatch.setattr("app.local_asr_api.transcribe", slow)
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = upload(client, sid).json()["id"]
        assert started.wait(1)
        assert upload(client, sid).status_code == 503
        job = client.post(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}/cancel").json()
        assert job["status"] == "cancelled"
        finish.set()
        time.sleep(0.05)
        job = wait(client, sid, jid)
        assert job["text"] is None and job["status"] == "cancelled"


def test_changed_conversation_discards_result(settings, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    monkeypatch.setattr("app.local_asr_api.available", lambda _: True)
    def slow(*args):
        started.set()
        finish.wait(2)
        return "late result"
    monkeypatch.setattr("app.local_asr_api.transcribe", slow)
    app = create_app(settings)
    with TestClient(app) as client:
        sid = session(client)
        jid = upload(client, sid).json()["id"]
        started.wait(1)
        with app.state.sessions.begin() as db:
            db.get(Conversation, sid).revision += 1
        finish.set()
        time.sleep(0.05)
        assert client.get(f"/api/v1/sessions/{sid}/transcription-jobs/{jid}").status_code == 409
        with app.state.sessions() as db:
            assert db.get(TranscriptionJob, jid).text is None


def test_sensitive_transcript_is_not_persisted(settings, monkeypatch):
    monkeypatch.setattr("app.local_asr_api.available", lambda _: True)
    monkeypatch.setattr("app.local_asr_api.transcribe", lambda *args: "我的验证码是123456")
    with TestClient(create_app(settings)) as client:
        sid = session(client)
        jid = upload(client, sid).json()["id"]
        job = wait(client, sid, jid)
        assert job["status"] == "failed" and job["text"] is None


def test_incomplete_restart_never_replays_audio(settings, monkeypatch):
    app = create_app(settings)
    with TestClient(app) as client:
        sid = session(client)
        with app.state.sessions.begin() as db:
            db.add(TranscriptionJob(id="interrupted", session_id=sid, revision=0, status="running",
                                    created_at=time.time(), confirmed=False))
        folder = temp_root(settings) / "turn-interrupted"
        folder.mkdir()
        (folder / "normalized.wav").write_bytes(b"ephemeral")
        cookie = client.cookies.get("bank_demo_owner")
    def forbidden(*args):
        pytest.fail("Restart must never repeat ASR")
    monkeypatch.setattr("app.local_asr_api.transcribe", forbidden)
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set("bank_demo_owner", cookie)
        job = restarted.get(f"/api/v1/sessions/{sid}/transcription-jobs/interrupted").json()
        assert job["status"] == "failed" and job["error_code"] == "RESTART_INTERRUPTED"
        assert not folder.exists()


def test_disabled_and_bad_input_errors_are_structured(settings):
    with TestClient(create_app(replace(settings, asr_enabled=False))) as client:
        sid = session(client)
        response = upload(client, sid)
        assert response.status_code == 503 and response.json()["error"]["code"] == "ASR_UNAVAILABLE"
        response = client.post(f"/api/v1/sessions/{sid}/transcription-jobs", content=b"anything")
        assert response.status_code == 422 and "error" in response.json()
        assert not client.get("/api/v1/capabilities").json()["local_asr"]
