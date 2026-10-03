"""Controlled public demo services; no account tools, URLs, or generated business steps."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from app.schemas import (
    Answer,
    CardSource,
    CustomerType,
    Knowledge,
    NavigationAction,
    NavigationState,
    ServiceCard,
    ServiceItem,
    ServiceView,
    StrictModel,
)
from app.service import eligible


class ServiceConfigUnavailable(Exception):
    pass


class CapabilityChanged(Exception):
    pass


class CatalogConfig(StrictModel):
    capability_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=100)
    customer_type: CustomerType
    knowledge_id: str = Field(min_length=1, max_length=80)
    knowledge_version: str = Field(min_length=1, max_length=80)
    enabled: bool = Field(strict=True)
    question: str = Field(min_length=1, max_length=200)
    related_questions: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("title", "knowledge_id", "knowledge_version", "question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Blank configuration")
        return value

    @field_validator("related_questions")
    @classmethod
    def valid_questions(cls, value: list[str]) -> list[str]:
        if any(not text.strip() or len(text) > 200 for text in value):
            raise ValueError("Invalid fixed question")
        if len(set(value)) != len(value):
            raise ValueError("Duplicate fixed question")
        return value


def load_catalog(path: Path | None = None) -> list[CatalogConfig]:
    try:
        raw = json.loads((path or Path(__file__).with_name("service_catalog.json")).read_text())
        if not isinstance(raw, list):
            raise ValueError("A catalog must be a list")
        entries = [CatalogConfig.model_validate(item) for item in raw]
        if len({entry.capability_id for entry in entries}) != len(entries):
            raise ValueError("Duplicate capability")
        if len({entry.knowledge_id for entry in entries}) != len(entries):
            raise ValueError("Duplicate knowledge mapping")
        return entries
    except (OSError, ValueError, TypeError, ValidationError):
        raise ServiceConfigUnavailable from None


def available_entries(
    entries: list[CatalogConfig], items: list[Knowledge], customer_type: CustomerType,
) -> list[tuple[CatalogConfig, Knowledge]]:
    current = {item.id: item for item in eligible(items, customer_type) if item.text.strip()}
    return [(entry, current[entry.knowledge_id]) for entry in entries
            if entry.enabled and entry.customer_type == customer_type and entry.knowledge_id in current
            and entry.knowledge_version == current[entry.knowledge_id].version]


def resolve_service(
    entries: list[CatalogConfig], items: list[Knowledge], customer_type: CustomerType,
    capability_id: str | None, knowledge_version: str | None,
) -> tuple[CatalogConfig, Knowledge]:
    for entry, knowledge in available_entries(entries, items, customer_type):
        if entry.capability_id == capability_id and knowledge.version == knowledge_version:
            return entry, knowledge
    raise CapabilityChanged


def usable_knowledge(entries: list[CatalogConfig], items: list[Knowledge]) -> list[Knowledge]:
    return [item for kind in ("personal", "enterprise")
            for _, item in available_entries(entries, items, kind)]


def service_item(entry: CatalogConfig, question: str | None = None) -> ServiceItem:
    return ServiceItem(capability_id=entry.capability_id, title=entry.title,
                       customer_type=entry.customer_type, knowledge_id=entry.knowledge_id,
                       knowledge_version=entry.knowledge_version, question=question or entry.question)


def reading_steps(knowledge: Knowledge) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？])", knowledge.text) if part.strip()]


def knowledge_fingerprint(knowledge: Knowledge) -> str:
    return hashlib.sha256(knowledge.model_dump_json().encode("utf-8")).hexdigest()


def make_card(
    entry: CatalogConfig, knowledge: Knowledge, message_id: int | None = None,
    *, reading: bool = False, step_index: int = 0,
) -> ServiceCard:
    steps = reading_steps(knowledge)
    actions: list[NavigationAction] = ["start"]
    if reading:
        actions = []
        if step_index > 0:
            actions.append("back")
        if step_index + 1 < len(steps):
            actions.append("next")
        actions.append("cancel")
    return ServiceCard(
        id=f"{entry.capability_id}:{message_id if message_id is not None else 'pending'}",
        kind="reading_steps" if reading else "public_info", capability_id=entry.capability_id,
        message_id=message_id, customer_type=knowledge.customer_type,
        title="演示说明分步阅读" if reading else knowledge.title, text=knowledge.text,
        source=CardSource(id=knowledge.id, title=knowledge.title, version=knowledge.version,
                          customer_type=knowledge.customer_type, valid_from=knowledge.valid_from,
                          valid_until=knowledge.valid_until),
        steps=steps, step_index=step_index, actions=actions,
    )


def build_service_view(
    customer_type: CustomerType, revision: int, raw_navigation: dict[str, Any],
    entries: list[CatalogConfig], items: list[Knowledge],
) -> ServiceView:
    navigation = NavigationState.model_validate(raw_navigation).model_copy(update={"revision": revision})
    result = ServiceView(session_revision=revision, navigation=navigation,
                         catalog=[service_item(entry) for entry, _ in available_entries(entries, items, customer_type)])
    if navigation.status != "reading":
        return result
    try:
        entry, knowledge = resolve_service(entries, items, customer_type, navigation.capability_id,
                                           navigation.knowledge_version)
        if (knowledge.id != navigation.knowledge_id
                or navigation.source_fingerprint != knowledge_fingerprint(knowledge)
                or navigation.step_index >= len(reading_steps(knowledge))):
            raise CapabilityChanged
        result.cards = [make_card(entry, knowledge, navigation.message_id,
                                  reading=True, step_index=navigation.step_index)]
        result.suggestions = [service_item(entry, question) for question in entry.related_questions][:3]
    except CapabilityChanged:
        result.navigation = navigation.model_copy(update={"status": "invalid"})
    return result


def validate_history_card(
    card: ServiceCard, customer_type: CustomerType, entries: list[CatalogConfig], items: list[Knowledge],
) -> ServiceCard:
    try:
        _, knowledge = resolve_service(entries, items, customer_type, card.capability_id, card.source.version)
        if (knowledge.id != card.source.id or knowledge.customer_type != card.customer_type
                or knowledge.text != card.text or knowledge.title != card.source.title
                or knowledge.valid_from != card.source.valid_from
                or knowledge.valid_until != card.source.valid_until):
            raise CapabilityChanged
        return card
    except CapabilityChanged:
        return unavailable_card(card)


def unavailable_card(card: ServiceCard) -> ServiceCard:
    return card.model_copy(update={"valid": False, "status": "unavailable", "actions": [],
                                    "unavailable_reason": "来源或服务配置已失效，请重新选择当前有效目录。"})


def answer_card(
    answer: Answer, customer_type: CustomerType, entries: list[CatalogConfig], items: list[Knowledge],
) -> ServiceCard | None:
    if answer.kind != "answered" or len(answer.sources) != 1:
        return None
    source = answer.sources[0]
    for entry, knowledge in available_entries(entries, items, customer_type):
        if source.id == knowledge.id and source.version == knowledge.version:
            return make_card(entry, knowledge)
    return None


def confirm_answer_sources(
    answer: Answer, previous_entries: list[CatalogConfig], previous_items: list[Knowledge],
    entries: list[CatalogConfig], items: list[Knowledge],
) -> None:
    """A source selected before latency must still be identical at commit time."""
    for source in answer.sources:
        for customer_type in ("personal", "enterprise"):
            prior = next(((entry, knowledge) for entry, knowledge in
                          available_entries(previous_entries, previous_items, customer_type)
                          if knowledge.id == source.id and knowledge.version == source.version), None)
            if prior is None:
                continue
            current = resolve_service(entries, items, customer_type, prior[0].capability_id, source.version)
            if current != prior:
                raise CapabilityChanged
            break
        else:
            raise CapabilityChanged
