"""校验本候选包使用的结构约束及内容关联；不是通用 JSON Schema 引擎。"""

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(value, rule, schema, path):
    if "$ref" in rule:
        rule = schema["$defs"][rule["$ref"].split("/")[-1]]
    if "const" in rule:
        require(type(value) is type(rule["const"]) and value == rule["const"], path)
    if "enum" in rule:
        require(value in rule["enum"], path)
    types = {"object": dict, "array": list, "string": str, "null": type(None)}
    if "type" in rule:
        names = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        require(any(type(value) is types[name] for name in names), path + " 类型")
    if value is None:
        return
    if isinstance(value, dict):
        require(set(rule["required"]).issubset(value), path + " 缺字段")
        require(set(value).issubset(rule["properties"]), path + " 多余字段")
        for key, item in value.items():
            validate(item, rule["properties"][key], schema, path + "." + key)
    elif isinstance(value, list):
        require(len(value) >= rule.get("minItems", 0), path + " 空列表")
        if rule.get("uniqueItems"):
            require(len(value) == len(set(value)), path + " 重复项")
        for index, item in enumerate(value):
            validate(item, rule["items"], schema, f"{path}[{index}]")
    elif isinstance(value, str):
        require(len(value) >= rule.get("minLength", 0), path + " 空文本")
        if rule.get("format") == "date":
            require(date.fromisoformat(value).isoformat() == value, path + " 日期")
        if rule.get("format") == "uri":
            url = urlparse(value)
            hosts = {"hk.bankcomm.com", "www.hk.bankcomm.com", "eb.hk.bankcomm.com",
                     "www.bankcomm.com.hk", "www.bankcomm.com", "www.ifec.org.hk",
                     "www.dps.org.hk", "www.info.gov.hk", "www.hkmc.com.hk", "www.news.gov.hk"}
            require(url.scheme == "https" and url.hostname in hosts, path + " 官网链接")


def main():
    folder = Path(__file__).resolve().parent
    bundle = json.loads((folder / "knowledge-base.json").read_text())
    schema = json.loads((folder / "knowledge-base.schema.json").read_text())
    evaluation = json.loads((folder / "evaluation-cases.json").read_text())
    validate(bundle, schema, schema, "bundle")
    for group in ("sources", "articles", "exclusions"):
        ids = [item["id"] for item in bundle[group]]
        require(len(ids) == len(set(ids)), group + " 编号重复")
    sources = {item["id"]: item for item in bundle["sources"]}
    articles = {item["id"]: item for item in bundle["articles"]}
    used_sources = set()
    topics = set()
    for item in articles.values():
        require(set(item["source_ids"]).issubset(sources), item["id"] + " 来源缺失")
        used_sources.update(item["source_ids"])
        require(item["retrieved_on"] == bundle["retrieved_on"], item["id"] + " 核对日期")
        require(item["review_due_on"] > item["retrieved_on"], item["id"] + " 复核日期")
        if item["source_valid_until"]:
            require(item["source_valid_until"] >= item["retrieved_on"], item["id"] + " 已过期")
        if item["project_topic"]:
            topics.add(item["project_topic"])
            require(item["project_topic"].startswith(item["customer_type"] + "_"),
                    item["id"] + " 对象错配")
        if item["entity"] == "bank_hk_ltd":
            require(any(sources[sid]["entity"] == "bank_hk_ltd" for sid in item["source_ids"]),
                    item["id"] + " 银行实体依据缺失")
    require(used_sources == set(sources), "未关联来源")
    require(topics == {"personal_opening", "personal_deposit", "personal_repayment",
                       "enterprise_opening", "enterprise_reconciliation", "enterprise_financing"},
            "六类主题覆盖不完整")
    require(not set(sources) & {item["id"] for item in bundle["exclusions"]}, "排除来源混入")
    ids = [case["id"] for case in evaluation["cases"]]
    require(len(ids) == len(set(ids)), "验收用例重复")
    require(evaluation["model_calls_made"] == 0, "本任务不应调用模型")
    for case in evaluation["cases"]:
        require(set(case["relevant_article_ids"]).issubset(articles), case["id"] + " 知识缺失")
        require(case["execution_status"] in {"pending_integration", "passed_local_api_mapped"}, "未知验收状态")
        if case["execution_status"] == "passed_local_api_mapped":
            require(bool(case.get("executed_question") and case.get("customer_type") and case.get("test")),
                    "已执行验收缺少输入及测试定位")
            require(evaluation.get("verified_on") and evaluation.get("evidence"), "已执行验收缺少证据")
        require(bool(case["question"] and case["expected_behavior"]), "验收用例缺内容")
    print(f'{len(articles)}条知识、{len(sources)}个来源、{len(bundle["exclusions"])}项排除、'
          f'{len(evaluation["cases"])}条接入验收（状态见evaluation-cases.json）；资料校验通过')


if __name__ == "__main__":
    main()
