"""Source-bound Hong Kong bank FAQs; no inference between banks or live account actions."""

import hashlib
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.knowledge_base import business_date, knowledge_source
from app.public_answers import combine, question_focus
from app.schemas import (
    Answer,
    AnswerPart,
    ConsultationContext,
    CustomerType,
    Knowledge,
    PublicFact,
    PublicReference,
    StrictModel,
)
from app.service import eligible
from app.services_catalog import CatalogConfig, ServiceConfigUnavailable, knowledge_fingerprint

BankId = Literal["bocom", "hsbc", "hase", "bochk", "sc", "bea", "hkma"]
BANKS: dict[str, tuple[str, str, set[str]]] = {
    "bocom": (
        "交通银行（香港）有限公司",
        r"交通银行|交行|交银|BOCOM",
        {"hk.bankcomm.com", "www.hk.bankcomm.com"},
    ),
    "hsbc": ("香港上海汇丰银行有限公司", r"汇丰|HSBC", {"www.hsbc.com.hk"}),
    "hase": ("恒生银行有限公司", r"恒生|Hang\s*Seng", {"www.hangseng.com", "cms.hangseng.com"}),
    "bochk": ("中国银行（香港）有限公司", r"中银香港|中国银行.{0,3}香港|BOCHK", {"www.bochk.com"}),
    "sc": ("渣打银行（香港）有限公司", r"渣打|Standard\s*Chartered", {"www.sc.com"}),
    "bea": ("东亚银行有限公司（香港业务）", r"东亚|BEA", {"www.hkbea.com"}),
    "hkma": ("香港金融管理局（地区通用）", r"金管局|HKMA", {"www.hkma.gov.hk"}),
}
OFFICIAL_HOSTS = set().union(*(value[2] for value in BANKS.values()))


def normalized(text: str) -> str:
    pairs = {
        "滙豐": "汇丰",
        "匯豐": "汇丰",
        "恆生": "恒生",
        "渣打": "渣打",
        "東亞": "东亚",
        "中銀": "中银",
        "中國": "中国",
        "交通銀行": "交通银行",
        "轉數快": "转数快",
        "轉賬": "转账",
        "轉帳": "转账",
        "電子": "电子",
        "結單": "结单",
        "賬單": "账单",
        "網上銀行": "网上银行",
        "網銀": "网银",
        "手機": "手机",
        "密碼": "密码",
        "驗證碼": "验证码",
        "保安編碼": "保安编码",
        "戶口": "账户",
        "賬戶": "账户",
        "收款賬戶": "收款账户",
        "註冊": "注册",
        "登記": "登记",
        "開戶": "开户",
        "限額": "限额",
        "費用": "费用",
        "收費": "收费",
        "換": "换",
        "聯名": "联名",
        "郵": "邮",
        "下載": "下载",
        "支賬": "支账",
        "匯款": "汇款",
        "銀聯": "银联",
        "銀行": "银行",
        "認證": "认证",
    }
    for old, new in pairs.items():
        text = text.replace(old, new)
    return text


def named_banks(question: str) -> list[str]:
    text = normalized(question)
    matches = [
        (match.start(), key)
        for key, (_, pattern, _) in BANKS.items()
        if (match := re.search(pattern, text, re.IGNORECASE))
    ]
    return [key for _, key in sorted(matches)]


def wrong_region(question: str) -> bool:
    text = normalized(question)
    # The sender's generic bank location does not change the receiving HK bank.
    if named_banks(text) and "香港" in text and re.search(r"收.{0,20}汇款", text):
        text = re.sub(r"(?:内地|大陆|台湾|澳门|澳門|新加坡|马来西亚|英国|美国)银行", "汇出银行", text)
    regions = (
        r"内地|大陆|台湾|澳门|澳門|新加坡|马来西亚|英国|美国|"
        r"Singapore\b|Mainland\b|China\b|Taiwan\b|Macau\b|Malaysia\b|United\s*Kingdom\b|UK\b|USA\b"
    )
    banks = r"交通银行|交行|汇丰|渣打|恒生|东亚|中国银行|银行|HSBC|Standard\s*Chartered"
    return bool(
        re.search(
            r"香港分行|(?:"
            + regions
            + r")[）)\s]*(?:的)?(?:"
            + banks
            + r")|(?:"
            + banks
            + r")[（(\s]*(?:"
            + regions
            + r")",
            text,
            re.IGNORECASE,
        )
    )


class FaqSource(StrictModel):
    id: str
    bank_id: BankId
    entity: str
    title: str
    url: str
    jurisdiction: Literal["HK"]
    publisher: str
    verification: Literal["read_page", "read_browser"]
    retrieved_on: str
    published_on: str | None = None
    valid_until: str | None = None
    note: str

    @field_validator("retrieved_on", "published_on", "valid_until")
    @classmethod
    def dates(cls, value: str | None) -> str | None:
        from datetime import date

        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid date")
        return value


class FaqArticle(StrictModel):
    id: str = Field(pattern=r"^HKFAQ-[A-Z]+-[0-9]{2}$")
    bank_id: BankId
    evidence_role: Literal["bank_rule", "peer_guidance", "regional_guidance"] = "bank_rule"
    jurisdiction: Literal["HK"]
    customer_type: Literal["personal", "enterprise", "both"]
    topic: Literal["payments", "digital", "statements", "cards", "accounts", "security", "consent"]
    question: str = Field(min_length=1, max_length=200)
    variants: list[str] = Field(min_length=1, max_length=6)
    patterns: list[str] = Field(min_length=1, max_length=5)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=5)
    answer: str = Field(min_length=1, max_length=800)
    limits: str = Field(min_length=1, max_length=600)
    focus: Literal[
        "general", "materials", "channel", "fees", "qualification", "timing", "download", "guarantee"
    ]
    source_ids: list[str] = Field(min_length=1, max_length=3)
    claim_locator: str = Field(min_length=1)
    retrieved_on: str
    review_due_on: str
    bank_reviewed: Literal[False]

    @field_validator("retrieved_on", "review_due_on")
    @classmethod
    def dates(cls, value: str) -> str:
        FaqSource.dates(value)
        return value

    @field_validator("patterns", "exclude_patterns")
    @classmethod
    def regexes(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 240:
                raise ValueError("Invalid pattern")
            re.compile(value)
        return values


class FaqBundle(StrictModel):
    schema_version: Literal["1.0"]
    bundle_version: str = Field(max_length=40)
    jurisdiction: Literal["HK"]
    retrieved_on: str
    review_policy: str
    sources: list[FaqSource]
    articles: list[FaqArticle]
    exclusions: list[dict[str, str]]


class FaqActivation(StrictModel):
    version: str = Field(max_length=25)
    activated_on: str
    enabled_article_ids: list[str]


def faq_configuration(
    path: Path, activation_path: Path | None, entries: list[CatalogConfig], items: list[Knowledge]
) -> tuple[list[CatalogConfig], list[Knowledge]]:
    try:
        raw = path.read_bytes()
        activated = (activation_path or Path(__file__).with_name("bank_faq_activation.json")).read_bytes()
        bundle = FaqBundle.model_validate_json(raw)
        activation = FaqActivation.model_validate_json(activated)
        FaqSource.dates(activation.activated_on)
        current = business_date()
        if activation.activated_on > current:
            raise ValueError("Future activation")
        sources = {source.id: source for source in bundle.sources}
        articles = {article.id: article for article in bundle.articles}
        if (
            len(sources) != len(bundle.sources)
            or len(articles) != len(bundle.articles)
            or len(set(activation.enabled_article_ids)) != len(activation.enabled_article_ids)
            or not set(activation.enabled_article_ids).issubset(articles)
        ):
            raise ValueError("Duplicate or missing identifier")
        for source in bundle.sources:
            parsed = urlsplit(source.url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in BANKS[source.bank_id][2]
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or source.entity != BANKS[source.bank_id][0]
                or "/bhk/" in parsed.path
                or (source.bank_id == "sc" and not parsed.path.startswith("/hk/"))
            ):
                raise ValueError("Source bank or region mismatch")
        grouped: dict[tuple[CustomerType, str], list[FaqArticle]] = {}
        for article in bundle.articles:
            reference_banks = {sources[sid].bank_id for sid in article.source_ids if sid in sources}
            role = article.evidence_role
            valid_provenance = (
                reference_banks == {article.bank_id}
                if role == "bank_rule"
                else article.bank_id == "bocom"
                and (
                    (
                        role == "peer_guidance"
                        and bool(reference_banks - {"bocom", "hkma"})
                        and article.answer.startswith("建议：")
                        and article.limits.startswith("同业通用参考")
                        and not re.search(r"\d|SWIFT|Mobile Key|HSBCHK|免费|不收费|零收费", article.answer)
                    )
                    or (
                        role == "regional_guidance"
                        and reference_banks == {"hkma"}
                        and article.answer.startswith("香港通用知识：")
                        and article.limits.startswith("地区通用参考")
                    )
                )
            )
            if (
                not set(article.source_ids).issubset(sources)
                or not valid_provenance
                or article.review_due_on <= article.retrieved_on
            ):
                raise ValueError("Article provenance mismatch")
            if (
                article.id not in activation.enabled_article_ids
                or not (article.retrieved_on <= current < article.review_due_on)
                or any(
                    sources[sid].retrieved_on > current
                    or (sources[sid].valid_until is not None and current > str(sources[sid].valid_until))
                    for sid in article.source_ids
                )
            ):
                continue
            kinds: list[CustomerType] = (
                ["personal", "enterprise"] if article.customer_type == "both" else [article.customer_type]
            )
            for kind in kinds:
                grouped.setdefault((kind, article.bank_id), []).append(article)
        digest = hashlib.sha256(raw + activated).hexdigest()
        version = bundle.bundle_version + ":" + activation.version
        current_ids = {item.id for item in items}
        for (kind, bank), records in grouped.items():
            key = f"{kind}_faq_{bank}"
            if key in current_ids:
                raise ValueError("Knowledge collision")
            name = BANKS[bank][0]
            facts = [
                PublicFact(
                    id=record.id,
                    title=record.question,
                    focus=record.focus,
                    answer=record.answer,
                    limits=record.limits,
                    review_due_on=record.review_due_on,
                    references=[
                        PublicReference(
                            id=source.id,
                            title=source.title,
                            url=source.url,
                            publisher=source.publisher,
                            entity=source.entity,
                            retrieved_on=source.retrieved_on,
                            locator=(
                                (
                                    "同业通用参考，非交银条款："
                                    if record.evidence_role == "peer_guidance"
                                    else "香港地区公共知识，非交银专属条件："
                                    if record.evidence_role == "regional_guidance"
                                    else ""
                                )
                                + record.claim_locator
                            ),
                        )
                        for sid in record.source_ids
                        for source in [sources[sid]]
                    ],
                )
                for record in records
            ]
            items.append(
                Knowledge(
                    id=key,
                    customer_type=kind,
                    title=name + "客服常见问题",
                    version=version,
                    keywords=[record.question for record in records],
                    text="\n".join(f"{fact.title}：{fact.answer}\n适用限制：{fact.limits}" for fact in facts),
                    status="public_unreviewed",
                    valid_from=activation.activated_on,
                    valid_until=min(record.review_due_on for record in records),
                    public_facts=facts,
                    public_digest=digest,
                    faq_bank_id=bank,
                    faq_rules={record.id: record.patterns for record in records},
                    faq_exclusions={record.id: record.exclude_patterns for record in records},
                    faq_variants={record.id: [record.question, *record.variants] for record in records},
                    faq_guidance={record.id: record.evidence_role for record in records},
                )
            )
            examples = [
                (("香港通用知识：" if bank == "hkma" else name + "：") + record.question)
                for record in records[:3]
            ]
            entries.append(
                CatalogConfig(
                    capability_id=key,
                    knowledge_id=key,
                    customer_type=kind,
                    title=name.replace("有限公司", "")
                    .replace("香港上海汇丰银行", "汇丰香港")
                    .replace("（香港业务）", "")
                    + " · 常见问题",
                    knowledge_version=version,
                    enabled=True,
                    question=examples[0],
                    related_questions=examples,
                )
            )
        return entries, items
    except (OSError, ValueError, TypeError, KeyError):
        raise ServiceConfigUnavailable from None


def matching_facts(item: Knowledge, question: str) -> list[PublicFact]:
    text = normalized(question)
    text = re.sub(r"(?:不是|并非|不问|不要).*?(?:[，,]\s*(?:而是|是)|而是|是|我问的是)", "", text)
    text = re.sub(r"(?:不是|并非|不问|不要)[^，。；！？,;!?]*", "", text)
    canonical = [
        fact
        for fact in item.public_facts
        if any(normalized(variant) in text for variant in item.faq_variants.get(fact.id, []))
    ]
    matched = canonical or [
        fact
        for fact in item.public_facts
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in item.faq_rules.get(fact.id, []))
        and not any(
            re.search(pattern, text, re.IGNORECASE) for pattern in item.faq_exclusions.get(fact.id, [])
        )
    ]
    return [
        fact
        for fact in matched
        if not any(
            re.search(pattern, text, re.IGNORECASE) for pattern in item.faq_exclusions.get(fact.id, [])
        )
    ]


def faq_part(item: Knowledge, question: str, prior_ids: list[str] | None = None) -> AnswerPart:
    facts = matching_facts(item, question)
    focus = question_focus(normalized(question))
    # Facet-only follow-ups stay on the prior article, never select an unrelated bank FAQ.
    if (
        not facts
        and prior_ids
        and re.search(r"^(?:那|那么|这些|这个|它|还|需要|费用|多久|怎么|如何)", normalized(question))
    ):
        facts = [fact for fact in item.public_facts if fact.id in prior_ids]
        if focus != "general":
            facts = [fact for fact in facts if fact.focus == focus]
    name = BANKS[item.faq_bank_id or "bocom"][0]
    if len(facts) > 2:
        return AnswerPart(
            customer_type=item.customer_type,
            kind="unknown",
            processing="knowledge",
            coverage="none",
            text=f"{name}：同时涉及多个客服事项，请每次明确一至两个问题；不要发送凭证。",
        )
    if not facts:
        return AnswerPart(
            customer_type=item.customer_type,
            kind="unknown",
            processing="knowledge",
            coverage="none",
            text=f"{name}：已收录的官方资料未覆盖这一问题，不能套用其他银行、产品或地区的条件。请从该银行官方帮助入口核对，本次没有办理业务。",
        )
    gaps = []
    for facet, pattern in {
        "fees": r"利率|费用|收费|手续费|多少钱|报价",
        "timing": r"多久|几天|时限|到账时间",
        "materials": r"材料|证件|准备什么|哪些资料",
    }.items():
        if re.search(pattern, normalized(question)) and not any(fact.focus == facet for fact in facts):
            gaps.append(
                {
                    "fees": "所问具体费用或利率尚无适用依据，不能给出报价。",
                    "timing": "所问办理或到账时限尚无适用依据。",
                    "materials": "所问完整材料清单尚无适用依据。",
                }[facet]
            )
    fragments = re.split(r"[，。；,;]|(?:还有|以及|同时|和)", normalized(question))
    if any(
        re.search(r"按揭|房贷|保险|理财|积分|贷款|提款|开户|跨境|柜台", fragment)
        and not matching_facts(item, fragment)
        for fragment in fragments
    ):
        gaps.append("问题中另有未被本次来源覆盖的事项，请分开明确询问；本次不能作为完整答复。")
    if focus == "guarantee":
        gaps.append("不能承诺审批、转账成功、退款或资金追回。")
    text = f"适用：{name} · 香港 · {'个人' if item.customer_type == 'personal' else '企业'}服务\n\n"
    text += "\n\n".join(f"{fact.title}\n{fact.answer}\n适用限制：{fact.limits}" for fact in facts)
    if gaps:
        text += "\n\n资料缺口：" + "\n".join(gaps)
    text += "\n\n以上为官方公开资料整理，未获银行审核；本项目未办理、重置、转账、退款或查询真实账户。"
    return AnswerPart(
        customer_type=item.customer_type,
        kind="answered",
        processing="knowledge",
        focus=focus,
        coverage="partial" if gaps else "supported",
        text=text,
        sources=[knowledge_source(item, facts)],
    )


def faq_consultation(
    question: str, kind: CustomerType, items: list[Knowledge], context: ConsultationContext, mode: str
) -> tuple[Answer, ConsultationContext] | None:
    named = named_banks(question)
    if wrong_region(question) and (named or re.search(r"银行|开户|转账|网银", normalized(question))):
        return Answer(
            kind="unknown",
            mode="model" if mode == "model" else "demo",
            processing="knowledge",
            coverage="none",
            text="本轮补充资料适用香港银行；所问其他地区或交通银行香港分行的规则尚未覆盖，不能套用香港有限公司或其他银行条件。",
        ), ConsultationContext()
    if re.search(
        r"工商银行|建设银行|农业银行|招商银行|中国银行(?!.{0,3}香港)|花旗|星展|大新银行|"
        r"南洋商业银行|集友银行|众安银行|天星银行|富融银行|蚂蚁银行|民生银行|光大银行|"
        r"浦发银行|中信银行|Citibank|DBS|ZA\s*Bank|Mox\b|livi\b",
        normalized(question),
        re.IGNORECASE,
    ):
        return Answer(
            kind="unknown",
            mode="model" if mode == "model" else "demo",
            processing="knowledge",
            coverage="none",
            text="所问银行或地区尚未纳入本轮有效资料，请明确银行与地区；不会改用交银香港的条件。",
        ), ConsultationContext()
    candidates = [item for item in eligible(items, kind) if item.faq_bank_id]
    scope = re.search(r"个人|企业|对公", normalized(question))
    if scope:
        kind = "personal" if scope.group() == "个人" else "enterprise"
        candidates = [item for item in eligible(items, kind) if item.faq_bank_id]
    prior = [
        item
        for item in candidates
        if any(
            ref.knowledge_id == item.id
            and ref.knowledge_version == item.version
            and ref.source_fingerprint == knowledge_fingerprint(item)
            for ref in context.topics
        )
    ]
    if named:
        selected = [item for bank in named for item in candidates if item.faq_bank_id == bank]
        # The original BOCOM product facts still answer opening/deposit/loan topics.
        if named == ["bocom"] and not any(matching_facts(item, question) for item in selected):
            return None
    elif prior:
        selected = [item for item in prior if matching_facts(item, question)]
        if not selected:
            if len(prior) == 1 and re.search(
                r"^(?:那|那么|这些|这个|它|还|需要|费用|多久|怎么|如何)", normalized(question)
            ):
                selected = prior
            elif len(prior) == 1 and prior[0].faq_bank_id not in ("bocom", "hkma"):
                selected = prior
            else:
                return None
    else:
        selected = [
            item for item in candidates if item.faq_bank_id == "bocom" and matching_facts(item, question)
        ]
        if not selected:
            selected = [
                item for item in candidates if item.faq_bank_id == "hkma" and matching_facts(item, question)
            ]
    if not selected:
        if named:
            return Answer(
                kind="unknown",
                mode="model" if mode == "model" else "demo",
                processing="knowledge",
                coverage="none",
                text="该银行在当前服务对象下没有有效客服资料，或已停用/到复核日；请明确个人或企业问题并使用官方入口。",
            ), ConsultationContext()
        return None
    if len(selected) > 2:
        return Answer(
            kind="unknown",
            mode="model" if mode == "model" else "demo",
            processing="knowledge",
            coverage="none",
            text="请每次明确一至两间银行的客服问题；各行规则不能合并套用。",
        ), ConsultationContext()

    def bank_question(item: Knowledge) -> str:
        if len(named) < 2:
            return question
        text = normalized(question)
        starts = [
            (match.start(), bank)
            for bank in named
            for match in re.finditer(BANKS[bank][1], text, re.IGNORECASE)
        ]
        starts.sort()
        return "。".join(
            text[start : starts[index + 1][0] if index + 1 < len(starts) else len(text)]
            for index, (start, bank) in enumerate(starts)
            if bank == item.faq_bank_id
        )

    parts = [
        faq_part(
            item,
            bank_question(item),
            next((ref.article_ids for ref in context.topics if ref.knowledge_id == item.id), []),
        )
        for item in selected
    ]
    from app.schemas import TopicRef

    refs = [
        TopicRef(
            knowledge_id=item.id,
            customer_type=item.customer_type,
            knowledge_version=item.version,
            source_fingerprint=knowledge_fingerprint(item),
            article_ids=part.sources[0].article_ids if part.sources else [],
        )
        for item, part in zip(selected, parts, strict=True)
    ]
    return combine(parts, mode, "knowledge"), ConsultationContext(topics=refs)
