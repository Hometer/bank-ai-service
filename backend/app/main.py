import asyncio
import hashlib
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings
from app.consultation import answer_consultation, bind_topic, is_cancel, valid_topics
from app.db import Conversation, Message, SafetyNotice, cleanup_expired, open_database
from app.schemas import (
    Answer,
    ConsultationContext,
    CreateSession,
    CustomerType,
    DismissSafety,
    ErrorResponse,
    Health,
    History,
    Knowledge,
    MessageInput,
    MessageView,
    NavigationInput,
    NavigationState,
    SafetyState,
    ServiceView,
    SessionInfo,
    Turn,
)
from app.service import BLOCKED_TEXT, HUMAN, PRIVATE, URGENT, URGENT_NOTICE, load_knowledge, sanitize
from app.services_catalog import (
    CapabilityChanged,
    CatalogConfig,
    ServiceConfigUnavailable,
    answer_card,
    build_service_view,
    confirm_answer_sources,
    consultation_view,
    knowledge_fingerprint,
    load_catalog,
    make_card,
    reading_steps,
    resolve_service,
    source_bound_context,
    usable_knowledge,
    validate_history_card,
)

COOKIE = "bank_demo_owner"
RETENTION = 7 * 24 * 60 * 60
ERRORS: dict[int | str, dict[str, Any]] = {status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 503)}


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def session_view(item: Conversation) -> SessionInfo:
    return SessionInfo(id=item.id, customer_type=cast(CustomerType, item.customer_type),
                       created_at=iso(item.created_at), expires_at=iso(item.expires_at), revision=item.revision)


def safety_view(db: Session, owner: str) -> SafetyState:
    record = db.get(SafetyNotice, owner, populate_existing=True)
    if record is None:
        return SafetyState()
    active = record.active and record.expires_at > time.time()
    return SafetyState(active=active, revision=record.revision,
                       updated_at=iso(record.updated_at), expires_at=iso(record.expires_at),
                       text=URGENT_NOTICE if active else "")


def owner_hash(request: Request) -> str:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401)
    return hashlib.sha256(token.encode()).hexdigest()


def owned_session(db: Session, sid: str, owner: str) -> Conversation:
    item = db.get(Conversation, sid, populate_existing=True)
    if item is None or item.owner_hash != owner or item.expires_at <= time.time():
        raise HTTPException(404)
    return item


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.mode not in ("demo", "model"):
        raise ValueError("Unsupported model mode")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = open_database(settings.database_url)
        application.state.engine = engine
        application.state.sessions = sessionmaker(engine, expire_on_commit=False)
        try:
            application.state.knowledge = load_knowledge(settings.knowledge_path)
        except (OSError, ValueError, TypeError, ValidationError):
            application.state.knowledge = []
        # Default reads remain fresh; tests may explicitly replace this list as a provider override.
        application.state.initial_knowledge = application.state.knowledge
        cleanup_expired(engine)

        async def cleanup_loop() -> None:
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(cleanup_expired, engine)

        worker = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            engine.dispose()

    app = FastAPI(title="银行客服 · 本机演示", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    def configuration() -> tuple[list[CatalogConfig], list[Knowledge]]:
        entries = load_catalog(settings.catalog_path)
        try:
            if app.state.knowledge is not app.state.initial_knowledge:
                items = [Knowledge.model_validate(item) for item in app.state.knowledge]
            else:
                items = load_knowledge(settings.knowledge_path)
        except (OSError, ValueError, TypeError, ValidationError):
            raise ServiceConfigUnavailable from None
        return entries, items

    def current_services(item: Conversation, *, optional: bool = False) -> ServiceView | None:
        try:
            entries, items = configuration()
            return build_service_view(cast(CustomerType, item.customer_type), item.revision,
                                      item.navigation, entries, items, item.context)
        except ServiceConfigUnavailable:
            if optional:
                return None
            raise

    @app.middleware("http")
    async def safe_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": {"code": "ORIGIN_DENIED", "message": "请求来源不允许。"}}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "服务暂时不可用，请稍后重试。"}}, status_code=503)
        if response.status_code >= 400 and "application/json" not in response.headers.get("content-type", ""):
            response = JSONResponse({"error": {"code": "REQUEST_FAILED", "message": "请求无法完成。"}}, status_code=response.status_code)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "请检查输入类型、长度与必填项。"}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        messages = {401: ("AUTH_REQUIRED", "请先建立本机会话。"),
                    404: ("NOT_FOUND", "会话不存在、已过期或无权访问。"),
                    409: ("CONTEXT_CHANGED", "会话主题已更新，请刷新历史后重新咨询。"),
                    405: ("METHOD_NOT_ALLOWED", "此请求方法不支持。")}
        code, message = messages.get(exc.status_code, ("REQUEST_FAILED", "请求无法完成。"))
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=exc.status_code)

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(_: Request, __: SQLAlchemyError) -> JSONResponse:
        return JSONResponse({"error": {"code": "STORAGE_UNAVAILABLE", "message": "会话存储暂时不可用。"}}, status_code=503)

    @app.exception_handler(ServiceConfigUnavailable)
    async def service_configuration_error(_: Request, __: ServiceConfigUnavailable) -> JSONResponse:
        return JSONResponse({"error": {"code": "SERVICE_CONFIG_UNAVAILABLE",
                            "message": "演示服务资料暂不可用，请稍后重试。紧急提示仍可使用。"}}, status_code=503)

    @app.exception_handler(CapabilityChanged)
    async def changed_capability(_: Request, __: CapabilityChanged) -> JSONResponse:
        return JSONResponse({"error": {"code": "CAPABILITY_CHANGED",
                            "message": "演示服务或知识已更新，请刷新目录后重新选择。"}}, status_code=409)

    @app.get("/api/v1/health", response_model=Health, responses=ERRORS)
    def health() -> Health:
        with app.state.sessions() as db:
            db.execute(text("SELECT 1"))
        return Health.model_validate({"mode": settings.mode})

    @app.post("/api/v1/sessions", response_model=SessionInfo, status_code=201, responses=ERRORS)
    def create_session(body: CreateSession, request: Request, response: Response) -> SessionInfo:
        token = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
        now = time.time()
        item = Conversation(id=str(uuid4()), owner_hash=hashlib.sha256(token.encode()).hexdigest(),
                            customer_type=body.customer_type, created_at=now, expires_at=now + RETENTION)
        with app.state.sessions.begin() as db:
            db.add(item)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=RETENTION, path="/")
        return session_view(item)

    @app.get("/api/v1/sessions", response_model=list[SessionInfo], responses=ERRORS)
    def list_sessions(request: Request, customer_type: CustomerType | None = None) -> list[SessionInfo]:
        owner = owner_hash(request)
        with app.state.sessions() as db:
            query = select(Conversation).where(Conversation.owner_hash == owner, Conversation.expires_at > time.time())
            if customer_type:
                query = query.where(Conversation.customer_type == customer_type)
            items = db.scalars(query.order_by(Conversation.created_at.desc()).limit(50))
            return [session_view(item) for item in items]

    @app.get("/api/v1/sessions/{sid}", response_model=History, responses=ERRORS)
    def history(sid: str, request: Request) -> History:
        owner = owner_hash(request)
        with app.state.sessions() as db:
            item = owned_session(db, sid, owner)
            try:
                entries, items = configuration()
                services = build_service_view(cast(CustomerType, item.customer_type), item.revision,
                                              item.navigation, entries, items, item.context)
            except ServiceConfigUnavailable:
                entries, items, services = [], [], None
            messages = db.scalars(select(Message).where(Message.session_id == sid).order_by(Message.id))
            views: list[MessageView] = []
            for message in messages:
                answer = Answer.model_validate(message.answer) if message.answer else None
                if answer:
                    answer.cards = [validate_history_card(card, cast(CustomerType, item.customer_type),
                                                          entries, items) for card in answer.cards]
                views.append(MessageView(role=cast("Literal['user', 'assistant']", message.role),
                                         text=message.text, created_at=iso(message.created_at), answer=answer))
            context = source_bound_context(item.context, item.navigation, usable_knowledge(entries, items))
            return History(session=session_view(item), messages=views, services=services,
                           consultation=consultation_view(context, usable_knowledge(entries, items), item.revision))

    @app.get("/api/v1/sessions/{sid}/services", response_model=ServiceView, responses=ERRORS)
    def get_services(sid: str, request: Request) -> ServiceView:
        with app.state.sessions() as db:
            item = owned_session(db, sid, owner_hash(request))
            return cast(ServiceView, current_services(item))

    @app.post("/api/v1/sessions/{sid}/navigation", response_model=ServiceView, responses=ERRORS)
    def navigate(sid: str, body: NavigationInput, request: Request) -> ServiceView | JSONResponse:
        owner = owner_hash(request)
        with app.state.sessions.begin() as db:
            item = owned_session(db, sid, owner)
            if item.revision != body.expected_revision:
                raise HTTPException(409)
            customer_type = cast(CustomerType, item.customer_type)
            navigation = NavigationState.model_validate(item.navigation)
            answer: Answer | None = None
            question: str | None = None
            if body.action == "cancel":
                navigation = NavigationState(status="cancelled")
                context = ConsultationContext(clarification_status="cancelled")
            else:
                entries, items = configuration()
                if body.action == "start":
                    entry, knowledge = resolve_service(entries, items, customer_type,
                                                       body.capability_id, body.knowledge_version)
                    question = entry.question
                    answer = Answer(kind="answered", mode="demo", processing="rule", coverage="overview", text=knowledge.text,
                                    sources=[make_card(entry, knowledge).source])
                    navigation = NavigationState(status="reading", capability_id=entry.capability_id,
                                                 knowledge_id=knowledge.id, knowledge_version=knowledge.version,
                                                 source_fingerprint=knowledge_fingerprint(knowledge))
                else:
                    if navigation.status != "reading":
                        return JSONResponse({"error": {"code": "NAVIGATION_UNAVAILABLE",
                            "message": "当前没有可继续的阅读，请重新选择演示目录。"}}, status_code=409)
                    entry, knowledge = resolve_service(entries, items, customer_type,
                                                       navigation.capability_id, navigation.knowledge_version)
                    if navigation.source_fingerprint != knowledge_fingerprint(knowledge):
                        raise CapabilityChanged
                    index = navigation.step_index + (1 if body.action == "next" else -1)
                    if knowledge.id != navigation.knowledge_id or not 0 <= index < len(reading_steps(knowledge)):
                        return JSONResponse({"error": {"code": "NAVIGATION_UNAVAILABLE",
                            "message": "当前阅读位置没有这一步，请核对最新状态。"}}, status_code=409)
                    navigation = navigation.model_copy(update={"step_index": index})
                context = (ConsultationContext(topics=[bind_topic(knowledge)]) if body.action == "start" else
                           source_bound_context(item.context, item.navigation, usable_knowledge(entries, items)))
                latest_entries, latest_items = configuration()
                if resolve_service(latest_entries, latest_items, customer_type,
                                   entry.capability_id, knowledge.version) != (entry, knowledge):
                    raise CapabilityChanged
            result = db.execute(update(Conversation).where(
                Conversation.id == sid, Conversation.revision == body.expected_revision,
                Conversation.expires_at > time.time(),
            ).values(context=context.model_dump(), navigation=navigation.model_dump(),
                     revision=Conversation.revision + 1))
            if result.rowcount != 1:
                owned_session(db, sid, owner)
                raise HTTPException(409)
            if answer is not None and question is not None:
                now = time.time()
                db.add(Message(session_id=sid, role="user", text=question, created_at=now))
                message = Message(session_id=sid, role="assistant", text=answer.text, created_at=now)
                db.add(message)
                db.flush()
                answer.cards = [make_card(entry, knowledge, message.id)]
                message.answer = answer.model_dump()
                navigation.message_id = message.id
                item.navigation = navigation.model_dump()
            db.flush()
            db.refresh(item)
            services = current_services(item, optional=body.action == "cancel")
            if services is None:
                services = ServiceView(session_revision=item.revision,
                    navigation=NavigationState(status="cancelled", revision=item.revision),
                    consultation=consultation_view(context, [], item.revision))
            return services

    @app.get("/api/v1/safety", response_model=SafetyState, responses=ERRORS)
    def get_safety(request: Request) -> SafetyState:
        with app.state.sessions() as db:
            return safety_view(db, owner_hash(request))

    @app.post("/api/v1/safety/dismiss", response_model=SafetyState, responses=ERRORS)
    def dismiss_safety(body: DismissSafety, request: Request) -> SafetyState | JSONResponse:
        owner = owner_hash(request)
        with app.state.sessions.begin() as db:
            state = safety_view(db, owner)
            if state.revision != body.expected_revision:
                return JSONResponse({"error": {"code": "SAFETY_CHANGED", "message":
                    "出现了新的紧急提示，请核对最新提示后再决定是否关闭。"}}, status_code=409)
            if state.revision:
                result = db.execute(update(SafetyNotice).where(
                    SafetyNotice.owner_hash == owner, SafetyNotice.revision == body.expected_revision,
                ).values(active=False, revision=SafetyNotice.revision + 1, updated_at=time.time()))
                if result.rowcount != 1:
                    return JSONResponse({"error": {"code": "SAFETY_CHANGED", "message":
                        "紧急提示已发生变化，请刷新后核对。"}}, status_code=409)
            return safety_view(db, owner)

    @app.post("/api/v1/sessions/{sid}/messages", response_model=Turn, responses=ERRORS)
    def send_message(sid: str, body: MessageInput, request: Request) -> Turn:
        owner = owner_hash(request)
        urgent = any(word in body.text for word in URGENT)
        cancelling = is_cancel(body.text)
        protected_route = (urgent or sanitize(body.text) == BLOCKED_TEXT
                           or any(word in body.text for word in (*HUMAN, *PRIVATE)) or cancelling)
        with app.state.sessions() as db:
            item = owned_session(db, sid, owner)
            customer_type = cast(CustomerType, item.customer_type)
            raw_context, raw_navigation = item.context, item.navigation
            expected_revision = item.revision
            if not urgent and body.expected_revision is not None and body.expected_revision != item.revision:
                raise HTTPException(409)
        controlled_question = body.capability_id is not None and not urgent
        entries: list[CatalogConfig]
        items: list[Knowledge]
        mixed_private = (any(word in body.text for word in PRIVATE)
                         and not (urgent or sanitize(body.text) == BLOCKED_TEXT
                                  or any(word in body.text for word in HUMAN) or cancelling))
        if protected_route and not controlled_question and not mixed_private:
            entries, items = [], []
        else:
            try:
                entries, items = configuration()
            except ServiceConfigUnavailable:
                if controlled_question or not any(word in body.text for word in PRIVATE):
                    raise
                # A mixed question can retain its public answers when configuration is healthy.
                # If unavailable, the explicit private-query limitation still remains reachable.
                entries, items = [], []
        if controlled_question:
            entry, _ = resolve_service(entries, items, customer_type, body.capability_id, body.knowledge_version)
            if body.text not in [entry.question, *entry.related_questions]:
                raise CapabilityChanged
        context = source_bound_context(raw_context, raw_navigation, usable_knowledge(entries, items))
        safe_text, answer, next_context = answer_consultation(
            body.text, customer_type, usable_knowledge(entries, items), settings, context,
        )
        # Re-check after model latency, then persist the whole turn in one transaction.
        with app.state.sessions.begin() as db:
            item = owned_session(db, sid, owner)
            if answer.sources or not protected_route:
                latest_entries, latest_items = configuration()
                refs = next_context.topics + next_context.pending_topics
                if len(valid_topics(refs, usable_knowledge(latest_entries, latest_items))) != len(refs):
                    raise CapabilityChanged
            else:
                latest_entries, latest_items = entries, items
            if answer.sources:
                confirm_answer_sources(answer, entries, items, latest_entries, latest_items)
                if body.capability_id is not None:
                    entry, _ = resolve_service(latest_entries, latest_items, customer_type,
                                               body.capability_id, body.knowledge_version)
                    if body.text not in [entry.question, *entry.related_questions]:
                        raise CapabilityChanged
            card = answer_card(answer, customer_type, latest_entries, latest_items)
            navigation = NavigationState(status="cancelled" if cancelling else "idle")
            if answer.kind == "model_unavailable":
                retained = build_service_view(customer_type, item.revision, item.navigation,
                                              latest_entries, latest_items, item.context).navigation
                if (retained.status != "reading"
                        or retained.knowledge_id in {ref.knowledge_id for ref in next_context.topics}):
                    navigation = retained
            if card:
                _, selected_knowledge = resolve_service(latest_entries, latest_items, customer_type,
                                                        card.capability_id, card.source.version)
                navigation = NavigationState(status="reading", capability_id=card.capability_id,
                                             knowledge_id=card.source.id, knowledge_version=card.source.version,
                                             source_fingerprint=knowledge_fingerprint(selected_knowledge))
            mutation = update(Conversation).where(Conversation.id == sid, Conversation.expires_at > time.time())
            # Public urgent routing never depends on a prior topic. A concurrent ordinary
            # answer must not suppress the safety event; both turns remain atomic.
            if answer.kind != "urgent":
                mutation = mutation.where(Conversation.revision == expected_revision)
            result = db.execute(mutation.values(context=next_context.model_dump(), navigation=navigation.model_dump(),
                                                revision=Conversation.revision + 1))
            if result.rowcount != 1:
                owned_session(db, sid, owner)
                raise HTTPException(409)
            now = time.time()
            db.add(Message(session_id=sid, role="user", text=safe_text, created_at=now))
            message = Message(session_id=sid, role="assistant", text=answer.text, created_at=now)
            db.add(message)
            db.flush()
            if card:
                card.message_id = message.id
                card.id = f"{card.capability_id}:{message.id}"
                answer.cards = [card]
                navigation.message_id = message.id
                item.navigation = navigation.model_dump()
            message.answer = answer.model_dump()
            if answer.kind == "urgent":
                notice = insert(SafetyNotice).values(owner_hash=owner, active=True, revision=1,
                                                    updated_at=now, expires_at=item.expires_at)
                db.execute(notice.on_conflict_do_update(index_elements=[SafetyNotice.owner_hash], set_={
                    "active": True, "revision": SafetyNotice.revision + 1,
                    "updated_at": now, "expires_at": item.expires_at,
                }))
            state = safety_view(db, owner)
            db.flush()
            db.refresh(item)
            revision = item.revision
            services = current_services(item, optional=True)
        return Turn(user_text=safe_text, answer=answer, safety=state,
                    session_revision=revision, services=services,
                    consultation=consultation_view(next_context, usable_knowledge(latest_entries, latest_items), revision))

    static = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    @app.get("/app.js", include_in_schema=False)
    def script() -> FileResponse:
        return FileResponse(static / "app.js", media_type="text/javascript")

    @app.get("/app.css", include_in_schema=False)
    def stylesheet() -> FileResponse:
        return FileResponse(static / "app.css", media_type="text/css")

    return app


app = create_app()
