from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CustomerType = Literal["personal", "enterprise"]
Focus = Literal["general", "materials", "channel", "fees", "qualification", "timing", "download", "guarantee"]
Processing = Literal["rule", "model", "fallback", "legacy"]
Coverage = Literal["overview", "partial", "none", "legacy"]
ClarificationStatus = Literal["idle", "awaiting", "exhausted", "cancelled"]
Outcome = Literal[
    "answered", "unknown", "private_unavailable", "urgent", "human_unavailable",
    "sensitive_blocked", "model_unavailable", "clarification",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail


class Health(StrictModel):
    status: Literal["ok"] = "ok"
    mode: Literal["demo", "model"]
    real_model_verification: str = "待验"
    consultation_version: Literal["1.4"] = "1.4"


class CreateSession(StrictModel):
    customer_type: CustomerType


class SessionInfo(StrictModel):
    id: str
    customer_type: CustomerType
    created_at: str
    expires_at: str
    revision: int = 0


class MessageInput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    capability_id: str | None = Field(default=None, min_length=1, max_length=80)
    knowledge_version: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty input")
        return value.strip()

    @model_validator(mode="after")
    def paired_capability(self) -> "MessageInput":
        if (self.capability_id is None) != (self.knowledge_version is None):
            raise ValueError("Capability and version must be supplied together")
        return self


class Source(StrictModel):
    id: str
    title: str
    version: str
    status: Literal["demo_unreviewed"] = "demo_unreviewed"
    customer_type: CustomerType | None = None


NavigationAction = Literal["start", "next", "back", "cancel"]


class CardSource(Source):
    valid_from: str
    valid_until: str


class ServiceCard(StrictModel):
    id: str
    kind: Literal["public_info", "reading_steps"] = "public_info"
    capability_id: str
    message_id: int | None = None
    customer_type: CustomerType
    title: str
    text: str
    source: CardSource
    status: Literal["demo_available", "unavailable"] = "demo_available"
    valid: bool = True
    steps: list[str] = Field(default_factory=list)
    step_index: int = 0
    actions: list[NavigationAction] = Field(default_factory=list)
    unavailable_reason: str = ""


class ServiceItem(StrictModel):
    capability_id: str
    title: str
    customer_type: CustomerType
    knowledge_id: str
    knowledge_version: str
    status: Literal["demo_available"] = "demo_available"
    question: str


class NavigationState(StrictModel):
    status: Literal["idle", "reading", "cancelled", "invalid"] = "idle"
    capability_id: str | None = None
    knowledge_id: str | None = None
    knowledge_version: str | None = None
    source_fingerprint: str | None = None
    step_index: int = Field(default=0, ge=0)
    message_id: int | None = None
    revision: int = 0


class ServiceView(StrictModel):
    session_revision: int
    catalog: list[ServiceItem] = Field(default_factory=list)
    suggestions: list[ServiceItem] = Field(default_factory=list, max_length=3)
    navigation: NavigationState = Field(default_factory=NavigationState)
    cards: list[ServiceCard] = Field(default_factory=list)
    consultation: "ConsultationView" = Field(default_factory=lambda: ConsultationView())


class NavigationInput(StrictModel):
    action: NavigationAction
    expected_revision: int = Field(ge=0, strict=True)
    capability_id: str | None = Field(default=None, min_length=1, max_length=80)
    knowledge_version: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def action_fields(self) -> "NavigationInput":
        if self.action == "start":
            if self.capability_id is None or self.knowledge_version is None:
                raise ValueError("Starting requires capability and version")
        elif self.capability_id is not None or self.knowledge_version is not None:
            raise ValueError("Only starting accepts capability and version")
        return self


class AnswerPart(StrictModel):
    customer_type: CustomerType
    kind: Outcome
    text: str
    sources: list[Source] = Field(default_factory=list)
    processing: Processing = "legacy"
    focus: Focus = "general"
    coverage: Coverage = "legacy"


class Answer(StrictModel):
    kind: Outcome
    text: str
    mode: Literal["demo", "model"]
    sources: list[Source] = Field(default_factory=list)
    channel_status: str = "银行正式入口及人工队列尚未配置"
    parts: list[AnswerPart] = Field(default_factory=list)
    cards: list[ServiceCard] = Field(default_factory=list)
    processing: Processing = "legacy"
    focus: Focus = "general"
    coverage: Coverage = "legacy"


class TopicRef(StrictModel):
    knowledge_id: str
    customer_type: CustomerType
    knowledge_version: str | None = None
    source_fingerprint: str | None = None


class ConsultationContext(StrictModel):
    context_version: Literal[2] = 2
    topics: list[TopicRef] = Field(default_factory=list, max_length=2)
    pending_topics: list[TopicRef] = Field(default_factory=list, max_length=2)
    clarification_attempts: int = Field(default=0, ge=0, le=2, strict=True)
    clarification_status: ClarificationStatus = "idle"

    @model_validator(mode="before")
    @classmethod
    def legacy_status(cls, value: object) -> object:
        if isinstance(value, dict) and "clarification_status" not in value:
            value = dict(value)
            attempts = value.get("clarification_attempts", 0)
            value["clarification_status"] = "exhausted" if attempts == 2 else "awaiting" if attempts == 1 else "idle"
        return value


class ConsultationView(StrictModel):
    status: ClarificationStatus = "idle"
    clarification_attempts: int = Field(default=0, ge=0, le=2, strict=True)
    topic_ids: list[str] = Field(default_factory=list, max_length=2)
    pending_topic_ids: list[str] = Field(default_factory=list, max_length=2)
    can_cancel: bool = False
    revision: int = 0


class RouteItem(StrictModel):
    id: str = Field(min_length=1, strict=True)
    focus: Focus


class ConsultationRoute(StrictModel):
    action: Literal["select", "clarify", "unknown"]
    items: list[RouteItem] = Field(strict=True)

    @model_validator(mode="after")
    def action_items(self) -> "ConsultationRoute":
        if (self.action == "select") != bool(self.items):
            raise ValueError("Action and items disagree")
        return self


class SafetyState(StrictModel):
    active: bool = False
    revision: int = 0
    updated_at: str | None = None
    expires_at: str | None = None
    text: str = ""


class DismissSafety(StrictModel):
    expected_revision: int = Field(ge=0, strict=True)


class MessageView(StrictModel):
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    answer: Answer | None = None


class History(StrictModel):
    session: SessionInfo
    messages: list[MessageView]
    services: ServiceView | None = None
    consultation: ConsultationView = Field(default_factory=ConsultationView)


class Turn(StrictModel):
    user_text: str
    answer: Answer
    safety: SafetyState = Field(default_factory=SafetyState)
    session_revision: int = 0
    services: ServiceView | None = None
    consultation: ConsultationView = Field(default_factory=ConsultationView)


class Knowledge(StrictModel):
    id: str
    customer_type: CustomerType
    title: str
    version: str
    keywords: list[str]
    text: str
    status: Literal["demo_unreviewed", "disabled"]
    valid_from: str
    valid_until: str


class ModelSelection(StrictModel):
    knowledge_id: str | None
