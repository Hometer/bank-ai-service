"""Validated public research corpus, activated separately from archived candidates."""

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from app.schemas import CustomerType, Focus, Knowledge, PublicFact, PublicReference, Source, StrictModel
from app.services_catalog import CatalogConfig, ServiceConfigUnavailable

HOSTS = {"hk.bankcomm.com", "www.hk.bankcomm.com", "eb.hk.bankcomm.com", "www.bankcomm.com.hk",
         "www.ifec.org.hk", "www.dps.org.hk", "www.info.gov.hk", "www.news.gov.hk"}


def business_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


class ResearchSource(StrictModel):
    id: str
    title: str
    url: str
    entity: Literal["bank_hk_ltd", "hk_financial_education", "hk_public_rules"]
    jurisdiction: Literal["HK"]
    publisher: str
    verification: Literal["read_page", "read_browser", "search_excerpt"]
    locator: str
    published_on: str | None
    retrieved_on: str
    document_effective_from: str | None
    document_valid_until: str | None
    note: str

    @field_validator("url")
    @classmethod
    def official_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in HOSTS or url.username or url.password
                or url.port not in (None, 443)):
            raise ValueError("Non-official source")
        if "/bhk/" in url.path:
            raise ValueError("Branch source cannot replace Hong Kong subsidiary rules")
        return value

    @field_validator("published_on", "retrieved_on", "document_effective_from", "document_valid_until")
    @classmethod
    def dates(cls, value: str | None) -> str | None:
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid source date")
        return value


class ResearchArticle(StrictModel):
    id: str
    title: str
    customer_type: Literal["personal", "enterprise", "both"]
    entity: Literal["bank_hk_ltd", "hk_general"]
    jurisdiction: Literal["HK"]
    project_topic: str | None
    focus: Focus
    questions: list[str]
    answer: str = Field(min_length=1)
    limits: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    claim_locator: str
    retrieved_on: str
    source_effective_from: str | None
    source_valid_until: str | None
    review_due_on: str
    review_reason: str
    status: Literal["staged"]
    enabled: Literal[False]
    bank_reviewed: Literal[False]

    @field_validator("retrieved_on", "source_effective_from", "source_valid_until", "review_due_on")
    @classmethod
    def dates(cls, value: str | None) -> str | None:
        return ResearchSource.dates(value)


class ResearchBundle(StrictModel):
    schema_version: Literal["1.0"]
    bundle_version: str
    title: str
    retrieved_on: str
    primary_entity: Literal["交通银行（香港）有限公司"]
    jurisdiction: Literal["HK"]
    publication_status: Literal["staged_not_integrated"]
    review_policy: str
    sources: list[ResearchSource]
    articles: list[ResearchArticle]
    exclusions: list[dict[str, str]]


class Activation(StrictModel):
    version: str
    activated_on: str
    entity: Literal["bank_hk_ltd"]
    jurisdiction: Literal["HK"]
    enabled_article_ids: list[str]

    @field_validator("activated_on")
    @classmethod
    def dates(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid activation date")
        return value


TOPICS: dict[str, tuple[CustomerType, str, list[str]]] = {
    "personal_opening": ("personal", "个人开户公开知识", ["开户", "香港身份证", "护照"]),
    "personal_deposit": ("personal", "个人存款公开知识", ["定期", "存款", "通知存款"]),
    "personal_repayment": ("personal", "DreamCash私人贷款知识", ["贷款", "还款", "DreamCash", "冷静期"]),
    "enterprise_opening": ("enterprise", "企业开户公开知识", ["开户", "简易账户", "初创"]),
    "enterprise_reconciliation": ("enterprise", "企业网银与对账知识", ["对账", "回单", "电子结单", "企业网银"]),
    "enterprise_financing": ("enterprise", "企业融资公开知识", ["融资", "贷款", "担保", "信用证"]),
    "personal_borrowing": ("personal", "借贷成本与78法则", ["APR", "月平息", "78法则"]),
    "enterprise_borrowing": ("enterprise", "借贷成本与78法则", ["APR", "月平息", "78法则"]),
    "personal_protection": ("personal", "香港存款保障知识", ["存保", "存款保障", "结构性存款"]),
    "enterprise_protection": ("enterprise", "香港存款保障知识", ["存保", "存款保障", "结构性存款"]),
    "enterprise_exchange": ("enterprise", "企业外汇远期知识", ["外汇远期", "汇率风险"]),
}


def topic_ids(article: ResearchArticle) -> list[str]:
    if article.project_topic is not None:
        if article.project_topic not in TOPICS or TOPICS[article.project_topic][0] != article.customer_type:
            raise ValueError("Topic/customer mismatch")
        return [article.project_topic]
    if article.id in ("BCHK-F01", "BCHK-F02"):
        return ["personal_borrowing", "enterprise_borrowing"]
    if article.id in ("BCHK-F03", "BCHK-F04"):
        return ["personal_protection", "enterprise_protection"]
    if article.id == "BCHK-F05" and article.customer_type == "enterprise":
        return ["enterprise_exchange"]
    raise ValueError("Unknown supplemental topic")


def public_configuration(path: Path, activation_path: Path | None, entries: list[CatalogConfig],
                         legacy: list[Knowledge]) -> tuple[list[CatalogConfig], list[Knowledge]]:
    """Fresh immutable snapshots; any broken configuration fails closed, without demo substitution."""
    try:
        raw = path.read_bytes()
        activated = (activation_path or Path(__file__).with_name("public_activation.json")).read_bytes()
        bundle = ResearchBundle.model_validate_json(raw)
        activation = Activation.model_validate_json(activated)
        current = business_date()
        if activation.activated_on > current:
            raise ValueError("Not active yet")
        sources = {source.id: source for source in bundle.sources}
        articles = {article.id: article for article in bundle.articles}
        if (len(sources) != len(bundle.sources) or len(articles) != len(bundle.articles)
                or len(set(activation.enabled_article_ids)) != len(activation.enabled_article_ids)
                or not set(activation.enabled_article_ids).issubset(articles)):
            raise ValueError("Duplicate or missing identifiers")
        grouped: dict[str, list[PublicFact]] = {key: [] for key in TOPICS}
        for article in bundle.articles:
            targets = topic_ids(article)
            if not set(article.source_ids).issubset(sources):
                raise ValueError("Missing source")
            if (article.entity == "bank_hk_ltd"
                    and not any(sources[sid].entity == "bank_hk_ltd" for sid in article.source_ids)):
                raise ValueError("Wrong bank entity")
            if article.id not in activation.enabled_article_ids:
                continue
            applicable = (article.retrieved_on <= current < article.review_due_on
                          and (article.source_effective_from is None or article.source_effective_from <= current)
                          and (article.source_valid_until is None or current <= article.source_valid_until))
            if not applicable:
                continue
            refs = []
            for sid in article.source_ids:
                source = sources[sid]
                if (source.verification == "search_excerpt" or source.retrieved_on > current
                        or (source.document_effective_from and source.document_effective_from > current)
                        or (source.document_valid_until and current > source.document_valid_until)):
                    applicable = False
                    break
                refs.append(PublicReference(id=source.id, title=source.title, url=source.url,
                    publisher=source.publisher, entity=source.entity, retrieved_on=source.retrieved_on,
                    locator=article.claim_locator))
            if applicable:
                fact = PublicFact(id=article.id, title=article.title, focus=article.focus, answer=article.answer,
                                  limits=article.limits, review_due_on=article.review_due_on, references=refs)
                for key in targets:
                    grouped[key].append(fact)
        digest = hashlib.sha256(raw + activated).hexdigest()
        version = bundle.bundle_version + ":" + activation.version
        result = {item.id: item for item in legacy}
        catalog = {entry.capability_id: entry for entry in entries}
        for key, (kind, title, keywords) in TOPICS.items():
            facts = grouped[key]
            text = "\n".join(f"{fact.title}：{fact.answer}\n资料限制：{fact.limits}" for fact in facts)
            until = min((fact.review_due_on for fact in facts), default=current)
            result[key] = Knowledge(id=key, customer_type=kind, title=title, version=version, keywords=keywords,
                text=text or "资料已停用或需复核，暂不能引用。", status="public_unreviewed" if facts else "disabled",
                valid_from=activation.activated_on, valid_until=until, public_facts=facts, public_digest=digest)
            catalog[key] = CatalogConfig(capability_id=key, customer_type=kind, title=title, knowledge_id=key,
                knowledge_version=version, enabled=bool(facts), question=f"{title}有哪些说明",
                related_questions=[article.questions[0] for article in bundle.articles
                                   if key in topic_ids(article) and article.id in {fact.id for fact in facts}][:3])
        return list(catalog.values()), list(result.values())
    except (OSError, ValueError, TypeError, KeyError):
        raise ServiceConfigUnavailable from None


def knowledge_source(item: Knowledge, facts: list[PublicFact] | None = None) -> Source:
    if item.status != "public_unreviewed":
        return Source(id=item.id, title=item.title, version=item.version, customer_type=item.customer_type)
    selected = item.public_facts if facts is None else facts
    references: dict[str, PublicReference] = {}
    for fact in selected:
        for ref in fact.references:
            existing = references.get(ref.id)
            if existing is None:
                references[ref.id] = ref
            elif ref.locator not in existing.locator.split("；"):
                references[ref.id] = existing.model_copy(update={"locator": existing.locator + "；" + ref.locator})
    return Source(id=item.id, title=item.title, version=item.version, status="public_unreviewed",
                  customer_type=item.customer_type, article_ids=[fact.id for fact in selected],
                  references=list(references.values()), retrieved_on=min((ref.retrieved_on for ref in references.values()),
                      default=None), review_due_on=min((fact.review_due_on for fact in selected), default=None))
