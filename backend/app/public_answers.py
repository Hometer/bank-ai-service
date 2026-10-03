"""Deterministic replies: source excerpts and explicit gaps, never generated bank facts."""

import re

from app.schemas import Answer, AnswerPart, Coverage, CustomerType, Focus, Knowledge, Processing, Source

FOCUS_RULES: list[tuple[Focus, str]] = [
    ("guarantee", r"肯定|保证|一定.*(?:批|成功)|能批|审批|额度|收益承诺|担保豁免"),
    ("download", r"下载|导出|获取回单"),
    ("fees", r"收费|费用|收多少|多少钱|利率|利息|收益|手续费|花多少"),
    ("materials", r"材料|带什么|准备什么|需要什么|哪些资料|什么证件"),
    ("channel", r"在线|线上|办理方式|渠道|去哪|哪里|网址|电话|网点"),
    ("qualification", r"资格|条件|要求.*一样|能不能办|可以申请"),
    ("timing", r"多久|时间|时限|几天|什么时候"),
]
GAPS: dict[Focus, str] = {
    "general": "现有资料仅提供演示概述，具体银行规则尚不能确认。",
    "materials": "现有演示资料无法确认具体材料清单。",
    "channel": "现有演示资料无法确认具体办理渠道或是否支持在线办理；银行正式入口尚未配置。",
    "fees": "现有演示资料无法确认具体费用、利率或收益，不能给出金额或收费结论。",
    "qualification": "现有演示资料无法确认具体申请资格或是否适用同样要求，自述身份不能授予权限。",
    "timing": "现有演示资料无法确认具体办理时限或放款时间。",
    "download": "当前未接入真实查询或下载服务，没有生成或下载回单。",
    "guarantee": "无法保证贷款或融资获批、额度或收益，本次没有审批或办理任何业务。",
}
LABELS: dict[CustomerType, str] = {"personal": "个人公开咨询", "enterprise": "企业公开咨询"}


def question_focus(text: str, suggested: Focus = "general") -> Focus:
    for focus, pattern in FOCUS_RULES:
        if re.search(pattern, text):
            return focus
    return suggested


def public_part(item: Knowledge, focus: Focus, processing: Processing, question: str = "",
                prior_ids: list[str] | None = None) -> AnswerPart:
    if item.status == "public_unreviewed":
        from app.public_retrieval import public_answer
        return public_answer(item, question, focus, prior_ids)
    sentences = [part for part in re.split(r"(?<=[。！？])", item.text) if part.strip()]
    excerpt = sentences[0] if sentences else item.text
    coverage: Coverage = "overview" if focus == "general" else "partial"
    text = f"关于{item.title}：{GAPS[focus]}\n相关原文：{excerpt}"
    if len(sentences) > 1:
        text += "\n资料限制：" + "".join(sentences[1:])
    return AnswerPart(customer_type=item.customer_type, kind="answered", text=text,
                      processing=processing, focus=focus, coverage=coverage,
                      sources=[Source(id=item.id, title=item.title, version=item.version,
                                      customer_type=item.customer_type)])


def unknown_part(customer_type: CustomerType, processing: Processing) -> AnswerPart:
    return AnswerPart(customer_type=customer_type, kind="unknown", processing=processing, coverage="none",
                      text=("知识库暂无这一公开问题的适用依据，或资料需要复核；请核对银行正式说明。本次未办理任何业务。"
                            if processing == "knowledge" else
                            "这一公开问题没有可用演示资料，无法确认具体规则；请查阅目标银行正式资料。本次未办理任何业务。"))


def combine(parts: list[AnswerPart], mode: str, processing: Processing) -> Answer:
    answered = [part for part in parts if part.kind == "answered"]
    coverage: Coverage = "supported" if parts and all(part.coverage == "supported" for part in parts) else (
        "partial" if answered and any(part.coverage != "overview" for part in parts) else (
        "overview" if answered else "none")
    )
    return Answer(kind="answered" if len(answered) == len(parts) else "unknown",
                  mode="model" if mode == "model" else "demo", processing=processing,
                  focus=parts[0].focus if len(parts) == 1 else "general", coverage=coverage, parts=parts,
                  text="\n\n".join(f"【{LABELS[part.customer_type]}】{part.text}" for part in parts),
                  sources=[source for part in parts for source in part.sources])
