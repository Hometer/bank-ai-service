"""Current-question routing with source-bound, durable public consultation state."""

import re
from typing import Literal

from app.config import Settings
from app.consultation_model import model_route
from app.public_answers import combine, public_part, question_focus, unknown_part
from app.schemas import (
    Answer,
    AnswerPart,
    ConsultationContext,
    ConsultationRoute,
    CustomerType,
    Knowledge,
    Processing,
    RouteItem,
    TopicRef,
)
from app.service import (
    BLOCKED_TEXT,
    HUMAN,
    URGENT,
    ModelInputTooLarge,
    ModelUnavailable,
    answer_question,
    eligible,
    private_request,
    sanitize,
)
from app.services_catalog import knowledge_fingerprint

SCOPE = re.compile(r"个人|企业|公司|对公")
CANCEL = {"取消", "算了", "不确定", "不清楚", "不知道", "暂时不问了"}
ALIASES = {
    "opening": r"开户|开卡|借记卡|对公账户|企业账户|日常收付款",
    "deposit": r"存款|定期|提前支取|没到期.*(?:取|钱)|(?:钱|存的).*取出来",
    "repayment": r"贷款|还款|提前还|借的钱|借款",
    "reconciliation": r"对账|回单",
    "financing": r"融资|贷款|借款|筹资",
}
PUBLIC_ALIASES = {
    **ALIASES,
    "opening": ALIASES["opening"] + r"|香港身份证|护照|非永久居民|简易账户|初创|注册|有限公司|支票账户",
    "deposit": ALIASES["deposit"] + r"|定存|续存|起存",
    "repayment": ALIASES["repayment"] + r"|DreamCash|私人贷款|自雇|佣金|冷静期|提款翌日",
    "reconciliation": ALIASES["reconciliation"] + r"|网银|网上银行|手机银行|电子结单|对账单|付款凭证|指令|预约转账|法人|岗位|角色|企业交易",
    "financing": ALIASES["financing"] + r"|八成|九成|中小企|信用证|贸易|进口|还息不还本|过渡资金",
    "borrowing": r"APR|实际年利率|月平息|78法则|贷款后期|借贷成本|提前.*省.*利息",
    "protection": r"存保|存款保障|结构性存款|80万.*保障|八十万.*保障|基金.*保障|保险.*保障|外币.*保障",
    "exchange": r"外汇|远期|汇率风险",
}
UNSUPPORTED = re.compile(r"信用卡|积分|外汇|保险|理财")
CORRECTION = re.compile(r"不是|并非|不问|理解错了|问错了|我问的是")
VAGUE = re.compile(r"^(?:那|那么|这个|这些|它|具体|还需要|需要|会|可以|现在|有什么|怎么|材料呢|企业呢|个人呢|嗯|随便|都可以|再说|还是这个|同一个|说不清)")


def is_cancel(text: str) -> bool:
    return re.sub(r"[\s，。！？!?]", "", text) in CANCEL


def scope_of(text: str, fallback: CustomerType) -> CustomerType:
    match = SCOPE.search(text)
    return ("personal" if match.group() == "个人" else "enterprise") if match else fallback


def bind_topic(item: Knowledge, article_ids: list[str] | None = None) -> TopicRef:
    return TopicRef(knowledge_id=item.id, customer_type=item.customer_type,
                    knowledge_version=item.version, source_fingerprint=knowledge_fingerprint(item),
                    article_ids=article_ids or [])


def valid_topics(refs: list[TopicRef], items: list[Knowledge]) -> list[TopicRef]:
    current = {item.id: item for kind in ("personal", "enterprise") for item in eligible(items, kind)}
    return [ref for ref in refs if ref.knowledge_id in current
            and ref.customer_type == current[ref.knowledge_id].customer_type
            and ref.knowledge_version == current[ref.knowledge_id].version
            and ref.source_fingerprint == knowledge_fingerprint(current[ref.knowledge_id])]


def positive_text(text: str) -> str:
    # Negated topic phrases never participate in positive matching.
    cleaned = re.sub(r"(?:不是|并非|不问|不要).*?(?:[，,]\s*(?:而是|是)|而是|是|我问的是)", "", text)
    return re.sub(r"(?:不是|并非|不问|不要)[^，。；！？,;!?]*", "", cleaned)


def fragments(text: str, fallback: CustomerType) -> list[tuple[CustomerType, str]]:
    matches = list(SCOPE.finditer(text))
    groups: list[tuple[CustomerType, str]] = []
    if len(matches) > 1:
        for i, match in enumerate(matches):
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            fragment = text[0 if i == 0 else match.start():end]
            # Shared trailing topic such as “个人和企业开户”.
            if re.fullmatch(r"(?:个人|企业|公司|对公)(?:和|与|以及|、|\s)*", fragment):
                fragment = text[matches[-1].end():]
            groups.append((scope_of(match.group(), fallback), fragment))
    else:
        groups = [(scope_of(text, fallback), text)]
    return [(kind, part) for kind, group in groups
            for part in re.split(r"[，。；！？,;!?]|(?:和|以及|还有|同时)", group) if part.strip()]


def matches_for(text: str, kind: CustomerType, items: list[Knowledge]) -> list[Knowledge]:
    matched = []
    for item in eligible(items, kind):
        if item.faq_bank_id is not None:
            from app.bank_faq import matching_facts, named_banks
            banks = named_banks(text)
            if (item.faq_bank_id in banks or (not banks and item.faq_bank_id in {"bocom", "hkma"})) and matching_facts(item, text):
                matched.append(item)
            continue
        topic = item.id.split("_", 1)[1]
        public = item.status == "public_unreviewed"
        if UNSUPPORTED.search(text) and not (public and (
                topic == "protection" or topic == "exchange"
                or (topic == "financing" and "按证保险" in text))):
            continue
        aliases = PUBLIC_ALIASES if public else ALIASES
        if re.search(aliases.get(topic, r"(?!)"), text, re.IGNORECASE):
            matched.append(item)
    special = [item for item in matched if item.id.endswith(("_borrowing", "_protection", "_exchange"))]
    if (any(item.id == "personal_opening" and item.public_facts for item in matched)
            and re.search(r"(?:已有|已经).*存款账户.*开户", text)):
        matched = [item for item in matched if item.id != "personal_deposit"]
    return special or matched


def unknown_fragments(text: str, fallback: CustomerType, items: list[Knowledge]) -> list[tuple[CustomerType, str]]:
    parallel = bool(re.search(r"和|以及|还有|同时", text))
    return [(kind, fragment) for kind, fragment in fragments(text, fallback)
            if (UNSUPPORTED.search(fragment) and not matches_for(fragment, kind, items)) or (
                parallel and not matches_for(fragment, kind, items)
                and question_focus(fragment) == "general" and not VAGUE.search(fragment)
                and not re.search(r"了解什么|看什么|看哪|核对什么|各要|都要|说明$", fragment))]


def rule_route(text: str, kind: CustomerType, items: list[Knowledge],
               context: ConsultationContext) -> ConsultationRoute:
    cleaned = positive_text(text)
    selected: dict[str, RouteItem] = {}
    for target, fragment in fragments(cleaned, kind):
        for item in matches_for(fragment, target, items):
            selected[item.id] = RouteItem(id=item.id, focus=question_focus(fragment))
    if selected:
        if len(selected) > 2:
            return ConsultationRoute(action="clarify", items=[])
        return ConsultationRoute(action="select", items=list(selected.values()))
    if UNSUPPORTED.search(cleaned):
        return ConsultationRoute(action="unknown", items=[])
    refs = context.pending_topics or context.topics
    explicit = SCOPE.search(cleaned)
    if explicit:
        target = scope_of(cleaned, kind)
        refs = [ref for ref in refs if ref.customer_type == target]
    correction_without_topic = bool(CORRECTION.search(text))
    followup = bool(VAGUE.search(cleaned) or (refs and question_focus(cleaned) != "general")
                    or re.search(r"一样吗|也一样|肯定能批|没到期", cleaned))
    if not correction_without_topic and len(refs) == 1 and (followup or explicit):
        return ConsultationRoute(action="select", items=[RouteItem(id=refs[0].knowledge_id,
                                                                   focus=question_focus(cleaned))])
    return ConsultationRoute(action="clarify" if followup or correction_without_topic or explicit else "unknown", items=[])


def clarification(context: ConsultationContext, mode: str, processing: Processing = "rule") -> tuple[Answer, ConsultationContext]:
    refs = context.pending_topics or context.topics
    if context.clarification_attempts >= 2:
        return Answer(kind="unknown", mode="model" if mode == "model" else "demo", processing=processing,
                      coverage="none", text="当前无法确定所指业务。已发出两次澄清，请从有效目录选择主题或取消；也可明确提出新的公开问题。"), (
            ConsultationContext(pending_topics=refs, clarification_attempts=2, clarification_status="exhausted"))
    attempts = context.clarification_attempts + 1
    choices = "、".join(ref.knowledge_id.replace("personal_", "个人 ").replace("enterprise_", "企业 ")
                       for ref in refs)
    # Titles are translated before returning; identifiers are never presented as product instructions.
    names = {"opening": "开户", "deposit": "存款", "repayment": "贷款", "financing": "融资", "reconciliation": "对账回单",
             "borrowing": "借贷成本", "protection": "存款保障", "exchange": "外汇远期"}
    for identifier, title in names.items():
        choices = choices.replace(identifier, title)
    question = f"请明确指的是{choices}中的哪一个主题。" if choices else "请说明要咨询个人还是企业，以及开户、存款、贷款、对账回单或融资中的具体主题。"
    return Answer(kind="clarification", mode="model" if mode == "model" else "demo", processing=processing,
                  coverage="none", text=f"{question}这是第 {attempts}/2 次澄清，可随时取消；不要发送账户、密码或验证码。"), (
        ConsultationContext(pending_topics=refs, clarification_attempts=attempts, clarification_status="awaiting"))


def answer_consultation(text: str, customer_type: CustomerType, items: list[Knowledge], settings: Settings,
                        context: ConsultationContext) -> tuple[str, Answer, ConsultationContext]:
    safe = sanitize(text)
    mode: Literal["demo", "model"] = "model" if settings.mode == "model" else "demo"
    if safe == BLOCKED_TEXT or any(word in text for word in (*URGENT, *HUMAN)):
        safe, answer = answer_question(text, customer_type, [], settings)
        answer.processing, answer.coverage = "rule", "none"
        return safe, answer, ConsultationContext()
    if private_request(safe, public_knowledge=settings.public_knowledge_path is not None):
        private_parts: list[AnswerPart] = []
        public_ids: set[str] = set()
        public_gaps: set[tuple[CustomerType, str]] = set()
        for kind, fragment in fragments(safe, customer_type):
            if private_request(fragment, public_knowledge=settings.public_knowledge_path is not None):
                _, unavailable = answer_question(fragment, kind, [], settings)
                private_parts.append(AnswerPart(customer_type=kind, kind="private_unavailable",
                    text=unavailable.text, processing="rule", coverage="none"))
            else:
                matched = matches_for(fragment, kind, items)
                for item in matched:
                    if item.id not in public_ids:
                        private_parts.append(public_part(item, question_focus(fragment), "rule", fragment))
                        public_ids.add(item.id)
                if not matched and (kind, fragment) not in public_gaps:
                    private_parts.append(unknown_part(kind, "rule"))
                    public_gaps.add((kind, fragment))
        if len(public_ids) + len(public_gaps) > 2:
            limited: list[AnswerPart] = []
            included_limit = False
            for part in private_parts:
                if part.kind == "private_unavailable":
                    limited.append(part)
                elif not included_limit:
                    limited.append(AnswerPart(customer_type=part.customer_type, kind="unknown",
                        processing="rule", coverage="none", text="每次最多处理两个明确公开主题，请分批咨询；本次没有截取部分主题作为完整答复。"))
                    included_limit = True
            return safe, combine(limited, mode, "rule"), ConsultationContext()
        if public_ids or public_gaps:
            mixed_processing: Processing = "knowledge" if any(part.processing == "knowledge" for part in private_parts) else "rule"
            return safe, combine(private_parts, mode, mixed_processing), ConsultationContext()
        _, unavailable = answer_question(safe, customer_type, [], settings)
        unavailable.processing, unavailable.coverage = "rule", "none"
        return safe, unavailable, ConsultationContext()
    if is_cancel(safe):
        return safe, Answer(kind="unknown", mode=mode, processing="rule", coverage="none",
            text="已取消本次普通咨询与主题澄清。历史保留，未改变任何账户或业务状态，未关闭紧急提示。"), (
                ConsultationContext(clarification_status="cancelled"))
    if settings.public_knowledge_path is not None and settings.bank_faq_path is not None:
        from app.bank_faq import faq_consultation
        faq = faq_consultation(safe, customer_type, items, context, settings.mode)
        if faq is not None:
            return safe, *faq
    if any(word in safe for word in ("密码", "验证码", "口令")):
        return safe, Answer(kind="unknown", mode=mode, processing="rule", coverage="none", text=(
            "可以咨询密码重置或验证码接收的公开流程，但请勿发送凭证内容。"
            "本演示没有目标银行审核的处理资料，请使用你已有的银行正式渠道；本次未重置密码或验证验证码。")), ConsultationContext()
    old_refs = context.topics + context.pending_topics
    context = context.model_copy(update={"topics": valid_topics(context.topics, items),
                                         "pending_topics": valid_topics(context.pending_topics, items)})
    explicit_matches = any(matches_for(fragment, kind, items)
                           for kind, fragment in fragments(positive_text(safe), customer_type))
    if old_refs and not (context.topics or context.pending_topics) and not explicit_matches:
        return safe, Answer(kind="unknown", mode=mode, processing="rule", coverage="none",
            text="上一公开主题的来源已失效或无法证明有效，不能沿用。请明确主题或选择当前有效目录后重新咨询。"), context
    if CORRECTION.search(safe):
        context = context.model_copy(update={"topics": [], "pending_topics": []})
    explicit_ids = {item.id for kind, fragment in fragments(positive_text(safe), customer_type)
                    for item in matches_for(fragment, kind, items)}
    unknown_groups = unknown_fragments(positive_text(safe), customer_type, items)
    mentioned_types = {scope_of(match.group(), customer_type)
                       for match in SCOPE.finditer(positive_text(safe))}
    covered_types = {item.customer_type for item in items if item.id in explicit_ids}
    unresolved_types = mentioned_types - covered_types - {kind for kind, _ in unknown_groups}
    if explicit_ids and unresolved_types:
        answer, next_context = clarification(ConsultationContext(
            clarification_attempts=context.clarification_attempts), settings.mode)
        return safe, answer, next_context
    if len(explicit_ids) + len(set(unknown_groups)) > 2:
        return safe, Answer(kind="unknown", mode=mode, processing="rule", coverage="none",
            text="每次最多处理两个明确公开主题，请分批咨询；本次没有截取部分主题作为完整答复。"), context
    draft = rule_route(safe, customer_type, items, context)
    # Already ambiguous two-topic pointers and unproductive clarification replies are deterministic.
    definite_ambiguity = (not explicit_matches and not UNSUPPORTED.search(safe)
                          and ((len(context.topics + context.pending_topics) > 1 and draft.action == "clarify")
                               or (context.clarification_attempts > 0 and draft.action == "clarify")
                               or (CORRECTION.search(safe) and draft.action == "clarify")))
    processing: Processing = "rule"
    target_types = {scope_of(match.group(), customer_type) for match in SCOPE.finditer(positive_text(safe))}
    if not target_types:
        target_types = {ref.customer_type for ref in context.topics + context.pending_topics} or {customer_type}
    candidates = [item for kind in ("personal", "enterprise") if kind in target_types for item in eligible(items, kind)]
    knowledge_mode = settings.public_knowledge_path is not None
    if settings.mode == "model" and candidates and not definite_ambiguity and not knowledge_mode:
        try:
            route = model_route(safe, customer_type, candidates, context, settings)
            if route.action == "select" and explicit_ids and {item.id for item in route.items} != explicit_ids:
                raise ModelUnavailable("Route does not cover the explicit current topics")
            processing = "model"
        except ModelInputTooLarge:
            return safe, Answer(kind="model_unavailable", mode=mode, processing="fallback", coverage="none",
                text="问题较长，超出本次验证的输入范围。请缩短问题或分批咨询；没有发起模型请求，原有有效主题与澄清状态保留。"), context
        except ModelUnavailable:
            return safe, Answer(kind="model_unavailable", mode=mode, processing="fallback", coverage="none",
                text="模型未获得有效结果，已结束等待，没有生成可确认的业务答案。已保存的有效主题与澄清状态保留，可稍后重试或选择演示目录。"), context
    else:
        route = draft
        if knowledge_mode:
            processing = "knowledge"
    # A selection cannot defeat explicit unknown topics or unresolved multi-topic pointers.
    if UNSUPPORTED.search(safe) and not explicit_matches:
        route = ConsultationRoute(action="unknown", items=[])
    if route.action == "clarify":
        answer, next_context = clarification(context, settings.mode, processing)
        return safe, answer, next_context
    if route.action == "unknown":
        return safe, combine([unknown_part(customer_type, processing)], mode, processing), ConsultationContext()
    selected = {item.id: item for item in candidates}
    parts = []
    refs = []
    for routed in route.items:
        item = selected[routed.id]
        related = [fragment for target, fragment in fragments(positive_text(safe), customer_type)
                   if item in matches_for(fragment, target, items)]
        if item.public_facts and sum(selected[choice.id].customer_type == item.customer_type
                                     for choice in route.items) == 1:
            related += [fragment for target, fragment in fragments(positive_text(safe), customer_type)
                        if target == item.customer_type and not matches_for(fragment, target, items)
                        and question_focus(fragment) != "general"]
        question = "。".join(related) if related else safe
        prior_ids = next((ref.article_ids for ref in context.topics if ref.knowledge_id == item.id), [])
        part = public_part(item, question_focus(question, routed.focus), processing, question, prior_ids)
        parts.append(part)
        refs.append(bind_topic(item, part.sources[0].article_ids if part.sources else []))
    groups = fragments(positive_text(safe), customer_type)
    for kind, _ in unknown_groups:
        if not any(part.customer_type == kind and part.kind == "unknown" for part in parts):
            parts.append(unknown_part(kind, processing))
    def position(part: AnswerPart) -> int:
        for index, (kind, fragment) in enumerate(groups):
            if kind == part.customer_type and (any(kind == gap_kind and fragment == gap for gap_kind, gap in unknown_groups) if part.kind == "unknown" else
                    any(item.id == part.sources[0].id for item in matches_for(fragment, kind, items))):
                return index
        return len(groups)
    parts.sort(key=position)
    return safe, combine(parts, mode, processing), ConsultationContext(topics=refs)
