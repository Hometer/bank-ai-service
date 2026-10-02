"""Bounded public consultation rules; no identity inference or general semantic model."""

import re

from app.config import Settings
from app.schemas import Answer, AnswerPart, ConsultationContext, CustomerType, Knowledge, TopicRef
from app.service import BLOCKED_TEXT, HUMAN, URGENT, answer_question, eligible, sanitize

SCOPE = re.compile(r"个人|企业|公司|对公")
FOLLOW_UP = re.compile(r"^(?:那|那么|这个|这些|它|具体|还需要|需要什么|有什么|怎么准备|材料呢|企业呢|个人呢)")
CANCEL = {"取消", "算了", "不确定", "不清楚", "不知道", "暂时不问了"}
LABELS: dict[CustomerType, str] = {"personal": "个人公开咨询", "enterprise": "企业公开咨询"}


def scope_of(text: str, fallback: CustomerType) -> CustomerType:
    match = SCOPE.search(text)
    return ("personal" if match.group() == "个人" else "enterprise") if match else fallback


def clarification(context: ConsultationContext, mode: str) -> tuple[Answer, ConsultationContext]:
    attempts = min(2, context.clarification_attempts + 1)
    if attempts >= 2:
        return Answer(kind="unknown", mode="model" if mode == "model" else "demo", text=(
            "仍无法确认你要咨询的公开主题，请明确个人或企业及开户、存款、贷款、对账或融资等主题。"
            "也可使用你已有的银行正式渠道；本演示没有人工连接。"
        )), ConsultationContext(clarification_attempts=attempts)
    return Answer(kind="clarification", mode="model" if mode == "model" else "demo", text=(
        "请说明要咨询个人还是企业，以及具体主题。若有多个问题，请逐项说明；不要发送账户、密码或验证码。"
    )), ConsultationContext(clarification_attempts=attempts)


def labeled(answer: Answer, customer_type: CustomerType) -> Answer:
    part = AnswerPart(customer_type=customer_type, kind=answer.kind, text=answer.text, sources=answer.sources)
    return answer.model_copy(update={"text": f"【{LABELS[customer_type]}】{answer.text}", "parts": [part]})


def topic_refs(answers: list[tuple[CustomerType, Answer]]) -> list[TopicRef]:
    return [TopicRef(knowledge_id=source.id, customer_type=customer_type)
            for customer_type, answer in answers for source in answer.sources][:2]


def answer_consultation(
    text: str, customer_type: CustomerType, items: list[Knowledge], settings: Settings,
    context: ConsultationContext,
) -> tuple[str, Answer, ConsultationContext]:
    safe = sanitize(text)
    # Safety and explicit human routes precede object clarification; secrets never reach a model.
    if safe == BLOCKED_TEXT or any(word in text for word in (*URGENT, *HUMAN)):
        safe, answer = answer_question(text, customer_type, items, settings)
        return safe, answer, ConsultationContext()
    if re.sub(r"[\s，。！？!?]", "", safe) in CANCEL:
        return safe, Answer(kind="unknown", mode="model" if settings.mode == "model" else "demo", text=(
            "已取消本次主题澄清。可重新说明公开问题，或使用你已有的银行正式渠道；没有改变任何账户或业务状态。"
        )), ConsultationContext()
    if any(word in safe for word in ("密码", "验证码", "口令")):
        return safe, Answer(kind="unknown", mode="model" if settings.mode == "model" else "demo", text=(
            "可以咨询密码重置或验证码接收的公开流程，但请勿发送凭证内容。"
            "本演示没有目标银行审核的处理资料，请从你已有的官方 App 或正式网站查找对应流程或人工服务。"
            "本次未重置密码，也未验证验证码。"
        )), ConsultationContext()

    scopes = list(SCOPE.finditer(safe))
    types = {scope_of(match.group(), customer_type) for match in scopes}
    if len(types) > 1:
        # Only two public object groups are supported. Shared trailing topic may qualify both.
        fragments: dict[CustomerType, str] = {}
        for index, match in enumerate(scopes):
            end = scopes[index + 1].start() if index + 1 < len(scopes) else len(safe)
            key = scope_of(match.group(), customer_type)
            fragments[key] = fragments.get(key, "") + safe[match.start():end]
        replies: list[tuple[CustomerType, Answer]] = []
        for key, fragment in fragments.items():
            shared_topic = re.fullmatch(r"(?:个人|企业|公司|对公)(?:和|与|以及|、|\s)*", fragment)
            query = safe if shared_topic else fragment
            matching_parts = [item for item in eligible(items, key)
                              if any(word in query for word in item.keywords)]
            if len(matching_parts) > 1:
                answer, _ = clarification(ConsultationContext(), settings.mode)
            else:
                _, answer = answer_question(query, key, items, settings)
            replies.append((key, answer))
        parts = [AnswerPart(customer_type=key, kind=answer.kind, text=answer.text, sources=answer.sources)
                 for key, answer in replies]
        result = Answer(
            kind="answered" if all(part.kind == "answered" for part in parts) else "unknown",
            mode="model" if settings.mode == "model" else "demo", parts=parts,
            text="\n\n".join(f"【{LABELS[part.customer_type]}】{part.text}" for part in parts),
            sources=[source for part in parts for source in part.sources],
        )
        return safe, result, ConsultationContext(
            topics=topic_refs(replies) if all(part.kind == "answered" for part in parts) else [],
        )

    target = scope_of(safe, customer_type)
    candidates = eligible(items, target)
    matching = [item for item in candidates if any(word in safe for word in item.keywords)]
    if not matching and FOLLOW_UP.search(safe):
        if len(context.topics) != 1:
            answer, next_context = clarification(context, settings.mode)
            return safe, answer, next_context
        topic = context.topics[0]
        # An explicit new object must not reuse the previous object's topic.
        if scopes and target != topic.customer_type:
            answer, next_context = clarification(context, settings.mode)
            return safe, answer, next_context
        available = eligible(items, topic.customer_type)
        previous = next((item for item in available if item.id == topic.knowledge_id), None)
        if previous is None:
            return safe, Answer(kind="unknown", mode="model" if settings.mode == "model" else "demo",
                                text="上一公开主题的知识已失效或停用，不能继续给出业务结论。请查阅银行正式资料。"), ConsultationContext()
        _, answer = answer_question(previous.keywords[0], topic.customer_type, items, settings)
        return safe, labeled(answer, topic.customer_type), ConsultationContext(
            topics=topic_refs([(topic.customer_type, answer)]),
        )
    if len(matching) > 1:
        answer, next_context = clarification(context, settings.mode)
        return safe, answer, next_context
    safe, answer = answer_question(safe, target, items, settings)
    return safe, labeled(answer, target), ConsultationContext(topics=topic_refs([(target, answer)]))
