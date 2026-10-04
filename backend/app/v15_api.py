"""Recoverable, owner-bound answer jobs; never replay paid work on restart."""

import asyncio
import time
from threading import Event
from typing import Any, Callable, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import AnswerJob, Conversation, Message
from app.schemas import ConsultationContext, CustomerType, Knowledge, StrictModel
from app.service import BLOCKED_TEXT, HUMAN, URGENT, private_request, sanitize
from app.services_catalog import source_bound_context
from app.v15_answer import Result, run_answer


class JobInput(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=0, strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    intent: Literal["answer", "agent_assist"] = "answer"


class JobView(StrictModel):
    id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    revision: int
    result: Result | None = None


class Capabilities(StrictModel):
    generation: bool
    web: str = "official_registered_pages_only"
    voice: str = "browser_mandarin_opt_in"
    human: str = "draft_assistance_only"
    local_asr: bool = False


def job_view(job: AnswerJob) -> JobView:
    return JobView(
        id=job.id,
        status=cast(Any, job.status),
        revision=job.revision,
        result=Result.model_validate(job.result) if job.result else None,
    )


def recover(app: FastAPI) -> None:
    app.state.v15_tasks = set()
    app.state.v15_cancel = {}
    with app.state.sessions.begin() as db:
        db.execute(
            update(AnswerJob).where(AnswerJob.status.in_(["pending", "running"])).values(status="failed")
        )


def install(
    app: FastAPI,
    settings: Settings,
    configuration: Callable[[], Any],
    owned: Callable[[Session, str, str], Conversation],
    owner_hash: Callable[[Request], str],
) -> None:
    def valid_job(db: Session, sid: str, jid: str, request: Request) -> AnswerJob:
        owned(db, sid, owner_hash(request))
        job = db.get(AnswerJob, jid)
        if job is None or job.session_id != sid:
            raise HTTPException(404)
        return job

    async def work(
        jid: str, kind: CustomerType, items: list[Knowledge], context: ConsultationContext
    ) -> None:
        with app.state.sessions.begin() as db:
            job = db.get(AnswerJob, jid)
            if job is None or job.status != "pending":
                app.state.v15_cancel.pop(jid, None)
                return
            job.status = "running"
            question, intent = job.question, cast(Literal["answer", "agent_assist"], job.intent)
        cancelled = app.state.v15_cancel[jid]
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(run_answer, question, kind, items, context, settings, intent, cancelled),
                timeout=38,
            )
            _, current = configuration()
            evidence_urls = {ref.url for e in result.evidence for ref in e.source.references}
            current_urls = {
                ref.url for item in current for fact in item.public_facts for ref in fact.references
            }
            if not evidence_urls <= current_urls:
                raise ValueError("Source withdrawn")
            with app.state.sessions.begin() as db:
                job = db.get(AnswerJob, jid, populate_existing=True)
                if job is None or job.status != "running":
                    return
                updated = db.execute(
                    update(Conversation)
                    .where(
                        Conversation.id == job.session_id,
                        Conversation.revision == job.revision,
                        Conversation.expires_at > time.time(),
                    )
                    .values(revision=Conversation.revision + 1, context=result.context.model_dump())
                )
                if updated.rowcount != 1:
                    job.status = "cancelled"
                    return
                job.status, job.result = "completed", result.model_dump()
                job.revision += 1
                now = time.time()
                db.add(Message(session_id=job.session_id, role="user", text=question, created_at=now))
                db.add(
                    Message(
                        session_id=job.session_id,
                        role="assistant",
                        text=result.answer.text,
                        answer=result.answer.model_dump(),
                        created_at=now,
                    )
                )
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception:
            cancelled.set()
            with app.state.sessions.begin() as db:
                db.execute(
                    update(AnswerJob)
                    .where(AnswerJob.id == jid, AnswerJob.status.in_(["pending", "running"]))
                    .values(status="failed")
                )
        finally:
            app.state.v15_cancel.pop(jid, None)

    @app.get("/api/v1/capabilities", response_model=Capabilities)
    def capabilities() -> Capabilities:
        from app.local_asr import available

        return Capabilities(generation=settings.v15_enabled, local_asr=available(settings))

    @app.post("/api/v1/sessions/{sid}/answer-jobs", response_model=JobView, status_code=202)
    async def submit(sid: str, body: JobInput, request: Request) -> JobView:
        if not settings.v15_enabled:
            raise HTTPException(503)
        if (
            not body.text.strip()
            or sanitize(body.text) == BLOCKED_TEXT
            or any(t in body.text for t in URGENT)
            or private_request(body.text, public_knowledge=True)
            or (body.intent == "answer" and any(t in body.text for t in HUMAN))
            or (body.intent == "agent_assist" and any(t in body.text for t in ("密码是", "验证码是")))
        ):
            raise HTTPException(422)
        _, items = configuration()
        owner = owner_hash(request)
        with app.state.sessions.begin() as db:
            session = owned(db, sid, owner)
            duplicate = db.scalar(
                select(AnswerJob).where(AnswerJob.session_id == sid, AnswerJob.request_id == body.request_id)
            )
            if duplicate:
                if duplicate.question != body.text.strip() or duplicate.intent != body.intent:
                    raise HTTPException(409)
                return job_view(duplicate)
            if session.revision != body.expected_revision:
                raise HTTPException(409)
            if db.scalar(
                select(AnswerJob.id).where(
                    AnswerJob.session_id == sid, AnswerJob.status.in_(["pending", "running"])
                )
            ):
                raise HTTPException(409)
            # Bound global worker count and thread use before external calls.
            if len(app.state.v15_tasks) >= 2:
                raise HTTPException(503)
            context = source_bound_context(session.context, session.navigation, items)
            changed = db.execute(
                update(Conversation)
                .where(Conversation.id == sid, Conversation.revision == body.expected_revision)
                .values(revision=Conversation.revision + 1)
            )
            if changed.rowcount != 1:
                raise HTTPException(409)
            job = AnswerJob(
                id=str(uuid4()),
                session_id=sid,
                request_id=body.request_id,
                question=body.text.strip(),
                intent=body.intent,
                revision=body.expected_revision + 1,
                status="pending",
                created_at=time.time(),
            )
            db.add(job)
            db.flush()
            view = job_view(job)
            kind = cast(CustomerType, session.customer_type)
        task = asyncio.create_task(work(view.id, kind, items, context))
        app.state.v15_cancel[view.id] = Event()
        app.state.v15_tasks.add(task)
        task.add_done_callback(app.state.v15_tasks.discard)
        return view

    @app.get("/api/v1/sessions/{sid}/answer-jobs", response_model=list[JobView])
    def jobs(sid: str, request: Request) -> list[JobView]:
        with app.state.sessions() as db:
            owned(db, sid, owner_hash(request))
            return [
                job_view(job)
                for job in db.scalars(
                    select(AnswerJob)
                    .where(AnswerJob.session_id == sid)
                    .order_by(AnswerJob.created_at.desc())
                    .limit(10)
                )
            ]

    @app.get("/api/v1/sessions/{sid}/answer-jobs/{jid}", response_model=JobView)
    def get_job(sid: str, jid: str, request: Request) -> JobView:
        with app.state.sessions() as db:
            job = valid_job(db, sid, jid, request)
            if job.result:
                result = Result.model_validate(job.result)
                _, items = configuration()
                facts = {fact.id: fact for item in items for fact in item.public_facts}
                urls = {ref.url for fact in facts.values() for ref in fact.references}
                for evidence in result.evidence:
                    if evidence.origin == "knowledge":
                        fact = facts.get(evidence.id)
                        if fact is None or evidence.text != (fact.answer + "\n限制：" + fact.limits)[:800]:
                            raise HTTPException(409)
                    elif not all(ref.url in urls for ref in evidence.source.references):
                        raise HTTPException(409)
            return job_view(job)

    @app.post("/api/v1/sessions/{sid}/answer-jobs/{jid}/cancel", response_model=JobView)
    def cancel(sid: str, jid: str, request: Request) -> JobView:
        with app.state.sessions.begin() as db:
            job = valid_job(db, sid, jid, request)
            if job.status in ("pending", "running"):
                event = app.state.v15_cancel.get(jid)
                if event:
                    event.set()
                job.status = "cancelled"
                db.execute(
                    update(Conversation)
                    .where(Conversation.id == sid, Conversation.revision == job.revision)
                    .values(revision=Conversation.revision + 1)
                )
            return job_view(job)

    speech_lock = asyncio.Lock()

    @app.post("/api/v1/sessions/{sid}/answer-jobs/{jid}/speech", include_in_schema=False)
    async def speech(sid: str, jid: str, request: Request) -> Any:
        import shutil

        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask

        from app.v15_speech import synthesize

        if not settings.v15_enabled or speech_lock.locked():
            raise HTTPException(503)
        view = get_job(sid, jid, request)
        if view.status != "completed" or view.result is None:
            raise HTTPException(409)
        # Check that this answer is still the current turn before synthesizing.
        with app.state.sessions() as db:
            session = owned(db, sid, owner_hash(request))
            if session.revision != view.revision:
                raise HTTPException(409)
        async with speech_lock:
            try:
                directory, wave = await asyncio.to_thread(synthesize, view.result.answer.text)
            except Exception:
                raise HTTPException(503) from None
        return FileResponse(
            wave,
            media_type="audio/wav",
            filename="reply.wav",
            background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
        )

    @app.get("/v15.js", include_in_schema=False)
    def script() -> Any:
        from pathlib import Path

        from fastapi.responses import FileResponse

        return FileResponse(Path(__file__).with_name("static") / "v15.js", media_type="text/javascript")
