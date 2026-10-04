"""Validate curated FAQ provenance, source manifest, schema and actual activation."""

import hashlib
import importlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
faq = importlib.import_module("app.bank_faq")


def main() -> None:
    folder = Path(__file__).resolve().parent
    data = json.loads((folder / "knowledge-base.json").read_text())
    bundle = faq.FaqBundle.model_validate(data)
    assert json.loads((folder / "knowledge-base.schema.json").read_text()) == faq.FaqBundle.model_json_schema()
    manifest = json.loads((folder / "source-verification.json").read_text())
    source_ids = {source.id for source in bundle.sources}
    assert {item["source_id"] for item in manifest["sources"]} == source_ids
    assert len(manifest["sources"]) == len(source_ids)
    used = {sid for article in bundle.articles for sid in article.source_ids}
    assert used == source_ids, "Unreferenced or missing source"
    questions = [(a.bank_id, a.customer_type, a.question) for a in bundle.articles]
    assert len(questions) == len(set(questions)), "Duplicate question within scope"
    assert len(bundle.articles) == 116 and len(bundle.sources) == 22
    assert len({a.bank_id for a in bundle.articles}) == 7
    assert len({a.topic for a in bundle.articles}) == 7
    assert len({e["id"] for e in bundle.exclusions}) == 7
    assert all(not a.bank_reviewed for a in bundle.articles)
    source_map = {s.id: s for s in bundle.sources}
    for item in manifest["sources"]:
        source = source_map[item["source_id"]]
        assert item["url"] == source.url and item["retrieved_on"] == source.retrieved_on
        assert len(item["local_text_sha256"]) == 64 and item["local_text_bytes"] > 300
        expected = [a.claim_locator for a in bundle.articles if source.id in a.source_ids]
        assert item["locators"] == expected
        raw_path = ROOT / "data/bank-faq-research" / (source.id + ".txt")
        if raw_path.exists():
            assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == item["local_text_sha256"]
    _, items = faq.faq_configuration(folder / "knowledge-base.json", None, [], [])
    enabled = {fact.id for item in items for fact in item.public_facts}
    activation = faq.FaqActivation.model_validate_json(
        (ROOT / "backend/app/bank_faq_activation.json").read_bytes()
    )
    assert enabled == set(activation.enabled_article_ids), "An activated article is expired or unavailable"
    print(f"{len(enabled)}条有效FAQ、22个官方来源、7项排除；结构/归属/覆盖/核对记录通过")
    print("银行分布：" + str(dict(Counter(a.bank_id for a in bundle.articles))))


if __name__ == "__main__":
    main()
