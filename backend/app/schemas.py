from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CustomerType = Literal["personal", "enterprise"]
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

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty input")
        return value.strip()


class Source(StrictModel):
    id: str
    title: str
    version: str
    status: Literal["demo_unreviewed"] = "demo_unreviewed"
    customer_type: CustomerType | None = None


class AnswerPart(StrictModel):
    customer_type: CustomerType
    kind: Outcome
    text: str
    sources: list[Source] = Field(default_factory=list)


class Answer(StrictModel):
    kind: Outcome
    text: str
    mode: Literal["demo", "model"]
    sources: list[Source] = Field(default_factory=list)
    channel_status: str = "银行正式入口及人工队列尚未配置"
    parts: list[AnswerPart] = Field(default_factory=list)


class TopicRef(StrictModel):
    knowledge_id: str
    customer_type: CustomerType


class ConsultationContext(StrictModel):
    topics: list[TopicRef] = Field(default_factory=list, max_length=2)
    clarification_attempts: int = Field(default=0, ge=0, le=2)


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


class Turn(StrictModel):
    user_text: str
    answer: Answer
    safety: SafetyState = Field(default_factory=SafetyState)


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
