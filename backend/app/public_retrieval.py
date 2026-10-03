"""Small-corpus retrieval with product-specific coverage and explicit missing facts."""

import re

from app.knowledge_base import knowledge_source
from app.schemas import AnswerPart, Focus, Knowledge, PublicFact

MATERIAL_PATTERN = r"材料|带什么|准备什么|需要什么(?!权限|角色|授权|条件|费用|利率|渠道|业务|账户)|哪些资料|什么证件"

PATTERNS = {
    "P01": r"资格|条件|护照|居民|身份证|改名|年龄|18岁",
    "P02": r"材料|准备|步骤|流程|操作|带什么|需要什么|哪些资料|在线|线上",
    "P03": r"已经|已有|现有客户|有账户|全新开户|还.*开户",
    "P04": r"支票|往来账户|哪些账户|账户类型|开.*账户",
    "P05": r"定期.*是什么|定期.*意思|定期.*期限|存定期.*选什么",
    "P06": r"起存|最低.*(?:多少|金额|存|门槛)|最少.*存|存.*最少|普通港元定存.*金额",
    "P07": r"通知存款|(?:一天|七天).*通知",
    "P08": r"人民币.*(?:续|到期)|(?:续|到期).*人民币",
    "P09": r"材料|文件|固定.*(?:工资|薪金|收入)|身份证明|住址证明",
    "P10": r"佣金|浮动|非固定|不固定",
    "P11": r"自雇|个体经营|自己做生意",
    "P12": r"提前.*(?:还|清)|(?:先还|还).*部分|结清",
    "P13": r"冷静期|七天|7天|提款翌日",
    "P14": r"提前.*(?:费|收|省钱)|(?:费用|手续费|收费).*提前",
    "E01": r"材料|文件|非本地|注册|法律形态|有限公司",
    "E02": r"简易|初创|单层|多层|架构|新成立",
    "E03": r"跨境|境外|海外|汇款|收.*款|简易.*服务",
    "E04": r"对账|结单|对账单|在哪|哪里|渠道|下载.*结单",
    "E05": r"法人|岗位|角色|权限|管理员|监督员|多级",
    "E06": r"手机银行|移动|流动|指令|预约|取消.*转账|授权.*转账",
    "E07": r"回单|凭证|签章|盖章",
    "E08": r"过渡|收购|投资项目|企业融资.*(?:做什么|用途|说明)",
    "E09": r"进口|贸易|信用证|信托收据|提货担保",
    "E10": r"中小企|担保计划|担保贷款|营运资金",
    "E11": r"八成|九成|还息不还本|担保.*(?:期限|截止|什么时候|多久)",
    "F01": r"APR|实际年利率|月平息|借贷成本|年利率",
    "F02": r"78|后期.*(?:还|利息)|提前.*省.*利息|利息.*分配",
    "F03": r"上限|80万|八十万|多少|每个账户|每间银行",
    "F04": r"结构性|保险|基金|外币|保障.*(?:范围|哪些)|哪些.*保障|受.*保障",
    "F05": r"远期|外汇|汇率|稳赚|赚钱|盈利",
}
DEFAULTS: dict[str, dict[Focus, list[str]]] = {
    "personal_opening": {"general": ["P01", "P02"], "materials": ["P02"], "qualification": ["P01"],
                         "channel": ["P02"]},
    "personal_deposit": {"general": ["P05"]},
    "personal_repayment": {"general": ["P09", "P12"], "materials": ["P09"]},
    "enterprise_opening": {"general": ["E01", "E02"], "materials": ["E01"]},
    "enterprise_reconciliation": {"general": ["E04", "E05"], "channel": ["E04"], "download": ["E07"]},
    "enterprise_financing": {"general": ["E08"]},
    "personal_borrowing": {"general": ["F01", "F02"]},
    "enterprise_borrowing": {"general": ["F01", "F02"]},
    "personal_protection": {"general": ["F03", "F04"]},
    "enterprise_protection": {"general": ["F03", "F04"]},
    "enterprise_exchange": {"general": ["F05"]},
}
MISSING: dict[Focus, str] = {
    "general": "知识库没有所问细节的适用依据，或相关资料已停用、到期需要复核。",
    "materials": "知识库没有该事项的完整材料依据，请按所引官方清单及银行补件要求核对。",
    "channel": "知识库没有确认该事项的具体办理入口；这里只能介绍公开流程，未办理业务。",
    "fees": "知识库没有所问利率或收费的适用依据，不能提供当日报价或本人费用。",
    "qualification": "知识库不能判断本人是否符合资格，或确认所问产品的全部申请条件。",
    "timing": "知识库没有所问事项的办理时限依据，不能承诺开户、审批或放款时间。",
    "download": "项目未接入真实查询或下载服务，没有生成或下载结单及回单。",
    "guarantee": "不能保证获批、授信、收益或办理成功，本次没有审批或执行任何业务。",
}


def retrieve(item: Knowledge, question: str, focus: Focus,
             prior_ids: list[str] | None = None) -> list[PublicFact]:
    # These records cover DreamCash, not mortgages, tax-loan offers or all loans.
    if item.id == "personal_repayment" and re.search(r"按揭|房贷|税贷|信用卡", question):
        return []
    facts = {fact.id.removeprefix("BCHK-"): fact for fact in item.public_facts}
    selected = [key for key in facts if re.search(PATTERNS[key], question, re.IGNORECASE)]
    if "P06" in selected and re.search(r"美元|人民币|外币|优惠|大额", question):
        selected.remove("P06")
    if item.id == "enterprise_financing" and "九成" in question:
        return []  # Never transfer an 80% programme deadline to a 90% programme.
    if "P02" in selected and re.search(r"网点|柜台|分行", question):
        selected.remove("P02")
    # The income category overrides the generic salaried documents.
    if any(key in selected for key in ("P10", "P11")):
        selected = [key for key in selected if key != "P09"]
    if (prior_ids and focus == "materials" and (not selected or selected == ["P09"])
            and not re.search(r"固定|受薪|薪金|工资|佣金|浮动|自雇|self.employed", question, re.IGNORECASE)):
        selected = [key for key in ("P10", "P11") if "BCHK-" + key in prior_ids and key in facts]
    if not selected:
        selected = [key for key in DEFAULTS.get(item.id, {}).get(focus, []) if key in facts]
        if re.search(r"网点|柜台|分行", question) and item.id == "personal_opening":
            selected = []
    # Prioritize a targeted fee/cooling/documents answer over a generic process paragraph.
    order = {"P14": 0, "P13": 0, "P11": 0, "P10": 0, "P03": 0, "P04": 0, "E11": 0, "E07": 0}
    selected.sort(key=lambda key: order.get(key, 1))
    return [facts[key] for key in selected[:3]]


def public_answer(item: Knowledge, question: str, focus: Focus,
                  prior_ids: list[str] | None = None) -> AnswerPart:
    if focus == "materials" and not re.search(MATERIAL_PATTERN, question):
        focus = "qualification" if re.search(r"条件|资格", question) else "general"
    wrong_bank = re.search(r"汇丰|恒生|中银香港|东亚银行|渣打|招商银行|工商银行|建设银行|香港分行", question)
    wrong_region = re.search(r"(?:内地|大陆|台湾|新加坡|伦敦)(?:的)?(?:交通银行|交行|银行)|"
                             r"(?:交通银行|交行)(?:内地|大陆|台湾)", question)
    if wrong_bank or wrong_region:
        return AnswerPart(customer_type=item.customer_type, kind="unknown", processing="knowledge",
            coverage="none", focus=focus, text="当前知识库对应交通银行（香港）有限公司及香港通用金融知识；"
            "所问其他银行、地区或香港分行的规则没有适用依据，不能混用。本次未办理业务。")
    facts = retrieve(item, question, focus, prior_ids)
    gaps = []
    if not facts:
        gaps.append(MISSING[focus])
    if focus == "guarantee" and item.id != "enterprise_reconciliation":
        gaps.append(MISSING["guarantee"])
    if focus == "download":
        gaps.append(MISSING["download"])
    # Matching a known topic is not evidence for every requested facet.
    covered = {fact.focus for fact in facts}
    requested: list[Focus] = []
    if re.search(r"利率|收费|费用|手续费|收多少|多少钱|报价", question):
        requested.append("fees")
    if re.search(r"多久|几天|时限|什么时候|截止|到什么时候", question):
        requested.append("timing")
    if re.search(MATERIAL_PATTERN, question):
        requested.append("materials")
    # APR is a concept, not a product quote; 78-rule content explains interest allocation.
    if item.id.endswith("_borrowing"):
        requested = []
    if (any(fact.id == "BCHK-P06" for fact in facts)
            and not re.search(r"利率|收费|费用|手续费|报价", question)):
        requested = [facet for facet in requested if facet != "fees"]
    for facet in requested:
        if facet not in covered and MISSING[facet] not in gaps:
            gaps.append(MISSING[facet])
    blocks = [f"{fact.title}：{fact.answer}\n适用限制：{fact.limits}" for fact in facts]
    if item.id == "personal_repayment":
        blocks.insert(0, "以下知识仅涉及DreamCash私人贷款，不能套用到按揭、税贷或其他贷款。")
    if gaps:
        blocks.append("资料缺口：" + "\n".join(gaps))
    blocks.append("以上为香港公开资料整理，未获银行审核；本项目没有办理、审批或查询真实账户。")
    return AnswerPart(customer_type=item.customer_type, kind="answered" if facts else "unknown",
        text="\n\n".join(blocks), processing="knowledge", focus=focus,
        coverage="partial" if facts and (gaps or any(fact.id in ("BCHK-E01", "BCHK-E07") for fact in facts))
        else "supported" if facts else "none", sources=[knowledge_source(item, facts)] if facts else [])
