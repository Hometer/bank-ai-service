"""Owner-bound offline transcription jobs, with recoverable review state."""

import asyncio
import time
from threading import Event
from typing import Any, Callable, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Conversation, TranscriptionJob
from app.local_asr import MAX_BYTES, ASRError, available, cleanup_interrupted, transcribe, validate_container
from app.schemas import StrictModel
from app.service import BLOCKED_TEXT, sanitize


class TranscriptionView(StrictModel):
    id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    revision: int
    text: str | None = None
    error_code: str | None = None
    confirmed: bool


class ConfirmInput(StrictModel):
    text: str = Field(min_length=1, max_length=1000)


def view(job: TranscriptionJob) -> TranscriptionView:
    return TranscriptionView(
        id=job.id, status=cast(Any, job.status), revision=job.revision,
        text=job.text, error_code=job.error_code, confirmed=job.confirmed,
    )


def recover(app: FastAPI, settings: Settings) -> None:
    app.state.asr_tasks = set()
    app.state.asr_cancel = {}
    if settings.v15_enabled:
        with app.state.sessions.begin() as db:
            db.execute(update(TranscriptionJob).where(
                TranscriptionJob.status.in_(["pending", "running"])
            ).values(status="failed", error_code="RESTART_INTERRUPTED"))
        cleanup_interrupted(settings)


def install(
    app: FastAPI, settings: Settings, owned: Callable[[Session, str, str], Conversation],
    owner_hash: Callable[[Request], str],
) -> None:
    upload_lock = asyncio.Lock()

    @app.exception_handler(ASRError)
    async def asr_error(_: Request, exc: ASRError) -> JSONResponse:
        messages = {
            "AUDIO_SIZE": (413, "录音过大，请控制在20秒内。"),
            "AUDIO_FORMAT": (415, "录音格式无法识别，请重新录音。"),
            "AUDIO_DURATION": (422, "录音需在0.25至20秒之间。"),
            "ASR_UNAVAILABLE": (503, "本机识别尚未准备好，请继续使用文字咨询。"),
            "UPLOAD_TIMEOUT": (408, "录音上传超时，请重新尝试。"),
        }
        status, message = messages.get(exc.code, (503, "本机语音识别未成功，请继续使用文字咨询。"))
        return JSONResponse({"error": {"code": exc.code, "message": message}}, status_code=status)

    def owned_job(db: Session, sid: str, jid: str, request: Request) -> tuple[Conversation, TranscriptionJob]:
        session = owned(db, sid, owner_hash(request))
        job = db.get(TranscriptionJob, jid)
        if job is None or job.session_id != sid:
            raise HTTPException(404)
        return session, job

    async def worker(jid: str, data: bytes, mime: str, cancelled: Event) -> None:
        try:
            with app.state.sessions.begin() as db:
                job = db.get(TranscriptionJob, jid)
                if job is None or job.status != "pending":
                    return
                job.status = "running"
            text = await asyncio.to_thread(transcribe, data, mime, settings, cancelled)
            with app.state.sessions.begin() as db:
                job = db.get(TranscriptionJob, jid)
                if job is None or job.status != "running":
                    return
                session = db.get(Conversation, job.session_id)
                if (cancelled.is_set() or session is None or session.revision != job.revision
                        or session.expires_at <= time.time()):
                    job.status = "cancelled"
                else:
                    # Defense in depth even if a future ASR adapter is introduced.
                    if not text.strip() or len(text) > 1000 or sanitize(text) == BLOCKED_TEXT:
                        raise ASRError("SENSITIVE_TRANSCRIPT")
                    job.status, job.text = "completed", sanitize(text)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ASRError) else "ASR_FAILED"
            with app.state.sessions.begin() as db:
                db.execute(update(TranscriptionJob).where(
                    TranscriptionJob.id == jid, TranscriptionJob.status.in_(["pending", "running"])
                ).values(status="failed", error_code=code))
        finally:
            app.state.asr_cancel.pop(jid, None)

    @app.post("/api/v1/sessions/{sid}/transcription-jobs", response_model=TranscriptionView, status_code=202)
    async def submit(sid: str, request: Request) -> TranscriptionView:
        with app.state.sessions() as db:
            session = owned(db, sid, owner_hash(request))
            header = request.headers.get("x-expected-revision", "")
            if not header.isascii() or not header.isdecimal() or len(header) > 10:
                raise HTTPException(422)
            revision = int(header)
            if revision != session.revision:
                raise HTTPException(409)
        if not available(settings):
            raise ASRError("ASR_UNAVAILABLE")
        if upload_lock.locked() or app.state.asr_tasks:
            raise HTTPException(503)
        async with upload_lock:
            length = request.headers.get("content-length")
            if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                raise ASRError("AUDIO_SIZE")
            data = bytearray()

            async def read() -> None:
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ASRError("AUDIO_SIZE")
            try:
                await asyncio.wait_for(read(), timeout=8)
            except asyncio.TimeoutError:
                raise ASRError("UPLOAD_TIMEOUT") from None
            mime = validate_container(bytes(data), request.headers.get("content-type", ""))
            with app.state.sessions.begin() as db:
                session = owned(db, sid, owner_hash(request))
                if session.revision != revision:
                    raise HTTPException(409)
                job = TranscriptionJob(
                    id=uuid4().hex, session_id=sid, revision=revision,
                    status="pending", created_at=time.time(), confirmed=False,
                )
                db.add(job)
                db.flush()
                result = view(job)
            cancelled = Event()
            task = asyncio.create_task(worker(result.id, bytes(data), mime, cancelled))
            app.state.asr_cancel[result.id] = cancelled
            app.state.asr_tasks.add(task)
            task.add_done_callback(app.state.asr_tasks.discard)
            return result

    @app.get("/api/v1/sessions/{sid}/transcription-jobs", response_model=list[TranscriptionView])
    def jobs(sid: str, request: Request) -> list[TranscriptionView]:
        with app.state.sessions() as db:
            session = owned(db, sid, owner_hash(request))
            return [view(job) for job in db.scalars(select(TranscriptionJob).where(
                TranscriptionJob.session_id == sid, TranscriptionJob.revision == session.revision
            ).order_by(TranscriptionJob.created_at.desc()).limit(10))]

    @app.get("/api/v1/sessions/{sid}/transcription-jobs/{jid}", response_model=TranscriptionView)
    def get_job(sid: str, jid: str, request: Request) -> TranscriptionView:
        with app.state.sessions() as db:
            session, job = owned_job(db, sid, jid, request)
            if session.revision != job.revision:
                raise HTTPException(409)
            return view(job)

    @app.post("/api/v1/sessions/{sid}/transcription-jobs/{jid}/cancel", response_model=TranscriptionView)
    def cancel(sid: str, jid: str, request: Request) -> TranscriptionView:
        with app.state.sessions.begin() as db:
            _, job = owned_job(db, sid, jid, request)
            event = app.state.asr_cancel.get(jid)
            if event:
                event.set()
            if not job.confirmed and job.status in ("pending", "running", "completed"):
                job.status, job.text = "cancelled", None
            return view(job)

    @app.post("/api/v1/sessions/{sid}/transcription-jobs/{jid}/confirm", response_model=TranscriptionView)
    def confirm(sid: str, jid: str, body: ConfirmInput, request: Request) -> TranscriptionView:
        with app.state.sessions.begin() as db:
            session, job = owned_job(db, sid, jid, request)
            if session.revision != job.revision or job.status != "completed":
                raise HTTPException(409)
            text = sanitize(body.text.strip())
            if not text or text == BLOCKED_TEXT:
                raise HTTPException(422)
            job.text, job.confirmed = text, True
            return view(job)

    @app.get("/local-voice.js", include_in_schema=False)
    def script() -> Any:
        from pathlib import Path

        from fastapi.responses import FileResponse

        return FileResponse(Path(__file__).with_name("static") / "local-voice.js", media_type="text/javascript")
