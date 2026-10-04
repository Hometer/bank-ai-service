"""Official supplemental FAQs: provenance, bank separation, durable context, no model calls."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bank_faq import BANKS, faq_configuration
from app.config import Settings
from app.main import create_app
from app.services_catalog import ServiceConfigUnavailable

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "docs/知识库/香港银行客服/knowledge-base.json"
ARTICLES = json.loads(CORPUS.read_text())["articles"]
PREFIX = {
    "bocom": "交银香港",
    "hsbc": "汇丰香港",
    "hase": "恒生",
    "bochk": "中银香港",
    "sc": "渣打香港",
    "bea": "东亚香港",
    "hkma": "香港金管局",
}


@pytest.fixture
def settings(tmp_path):
    for src, dest in [
        (CORPUS, "faq.json"),
        (ROOT / "backend/app/bank_faq_activation.json", "activation.json"),
    ]:
        shutil.copyfile(src, tmp_path / dest)
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'faq.sqlite3'}",
        public_knowledge_path=ROOT / "docs/知识库/交通银行香港/knowledge-base.json",
        bank_faq_path=tmp_path / "faq.json",
        bank_faq_activation_path=tmp_path / "activation.json",
    )


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr("app.consultation.model_route", lambda *a, **k: pytest.fail("No model call allowed"))
    with TestClient(create_app(settings)) as result:
        yield result


def new(client, kind="personal"):
    result = client.post("/api/v1/sessions", json={"customer_type": kind})
    assert result.status_code == 201
    return result.json()["id"]


def ask(client, sid, text):
    result = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text})
    assert result.status_code == 200, result.json()
    return result.json()


def ids(answer):
    return {item for source in answer["sources"] for item in source["article_ids"]}


def update(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.replace(path)


@pytest.mark.parametrize("article", ARTICLES, ids=[a["id"] for a in ARTICLES])
@pytest.mark.parametrize("variant", [False, True], ids=["canonical", "variant"])
def test_every_article_and_alternate_question_has_correct_fact_and_bank(client, article, variant):
    kind = "enterprise" if article["customer_type"] == "enterprise" else "personal"
    question = article["variants"][0] if variant else PREFIX[article["bank_id"]] + "：" + article["question"]
    answer = ask(client, new(client, kind), question)["answer"]
    assert article["id"] in ids(answer), (question, answer["text"])
    assert article["answer"] in answer["text"] and article["limits"] in answer["text"]
    assert answer["processing"] == "knowledge"
    assert BANKS[article["bank_id"]][0] in answer["text"]
    refs = [ref for source in answer["sources"] for ref in source["references"]]
    if article.get("evidence_role", "bank_rule") == "bank_rule":
        assert all(ref["entity"] == BANKS[article["bank_id"]][0] for ref in refs)
    else:
        assert "通用参考" in answer["text"]
        assert all("非交银" in ref["locator"] for ref in refs)
    assert all(ref["jurisdiction"] == "HK" and ref["retrieved_on"] == "2026-10-03" for ref in refs)


def test_default_traffic_bank_and_bank_switch_do_not_mix(client):
    sid = new(client)
    assert ids(ask(client, sid, "网银服务是否免费")["answer"]) == {"HKFAQ-BOCOM-07"}
    assert ids(ask(client, sid, "汇丰转数快有手续费吗")["answer"]) == {"HKFAQ-HSBC-12"}
    assert ids(ask(client, sid, "那费用呢")["answer"]) == {"HKFAQ-HSBC-12"}
    assert ids(ask(client, sid, "中银香港本地转数快收费吗")["answer"]) == {"HKFAQ-BOCHK-03"}
    assert ids(ask(client, sid, "交银香港开户需要什么材料")["answer"]) == {"BCHK-P02"}


@pytest.mark.parametrize(
    "question",
    [
        "汇丰新加坡开户材料",
        "HSBC Singapore 转数快收费吗",
        "Standard Chartered Malaysia 电子结单收费吗",
        "新加坡渣打转数快手续费",
        "交通银行香港分行开户材料",
        "工商银行开户材料",
        "大新银行转数快有手续费吗",
        "南洋商业银行网银免费吗",
        "中国银行开户材料",
    ],
)
def test_other_banks_regions_and_ambiguous_mainland_entity_never_inherit(client, question):
    answer = ask(client, new(client), question)["answer"]
    assert answer["sources"] == [] and answer["kind"] == "unknown"
    assert "香港身份证" not in answer["text"]


def test_unknown_facet_on_followup_not_unrelated_bank_answer(client):
    sid = new(client)
    ask(client, sid, "汇丰换手机怎样处理")
    answer = ask(client, sid, "那需要什么材料")["answer"]
    assert answer["sources"] == [] and answer["kind"] == "unknown"
    assert "香港上海汇丰银行" in answer["text"]
    assert "香港身份证" not in answer["text"]


@pytest.mark.parametrize(
    "question,kind",
    [
        ("验证码是123456", "sensitive_blocked"),
        ("密码是secret", "sensitive_blocked"),
        ("我的汇丰银行余额", "private_unavailable"),
        ("帮我查中银香港账单", "private_unavailable"),
        ("渣打银行卡被盗刷", "urgent"),
        ("汇丰转人工", "human_unavailable"),
    ],
)
def test_safety_and_private_paths_still_precede_official_faq(client, question, kind):
    sid = new(client)
    answer = ask(client, sid, question)["answer"]
    assert answer["kind"] == kind and answer["sources"] == []
    if kind == "sensitive_blocked":
        history = client.get(f"/api/v1/sessions/{sid}").text
        assert "123456" not in history and "secret" not in history


def test_no_free_model_call_when_mode_model(settings, monkeypatch):
    monkeypatch.setattr("app.consultation.model_route", lambda *a, **k: pytest.fail("No new model calls"))
    with TestClient(create_app(replace(settings, mode="model"))) as client:
        assert "HKFAQ-HSBC-03" in ids(ask(client, new(client), "汇丰网银密码怎样重置")["answer"])


def test_knowledge_counts_are_unique_articles_not_double_counted_both_scope(client):
    health = client.get("/api/v1/health").json()
    assert health["knowledge_article_count"] == 146 and health["knowledge_bank_count"] == 6


def test_cancel_drops_bank_context(client):
    sid = new(client)
    ask(client, sid, "汇丰转数快有手续费吗")
    ask(client, sid, "取消")
    answer = ask(client, sid, "那费用呢")["answer"]
    assert not any(i.startswith("HKFAQ-HSBC") for i in ids(answer))


def test_review_expiration_removes_all_supplemental_facts(client, monkeypatch):
    monkeypatch.setattr("app.bank_faq.business_date", lambda: "2026-11-03")
    answer = ask(client, new(client), "汇丰转数快有手续费吗")["answer"]
    assert answer["sources"] == [] and answer["kind"] == "unknown"


def test_disabled_prior_article_cannot_be_reused(client, settings):
    sid = new(client)
    ask(client, sid, "汇丰转数快有手续费吗")
    update(settings.bank_faq_activation_path, lambda d: d["enabled_article_ids"].remove("HKFAQ-HSBC-12"))
    answer = ask(client, sid, "那费用呢")["answer"]
    assert "HKFAQ-HSBC-12" not in ids(answer)
    assert "不对其FPS跨行收付款收费" not in answer["text"]


def test_changed_source_invalidates_prior_bank_context(client, settings):
    sid = new(client)
    ask(client, sid, "汇丰转数快有手续费吗")
    update(settings.bank_faq_path, lambda d: d["sources"][0].update(note="source metadata changed"))
    answer = ask(client, sid, "那费用呢")["answer"]
    assert "HKFAQ-HSBC-12" not in ids(answer)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d["sources"][0].update(url="https://evil.invalid/faq"),
        lambda d: d["sources"][0].update(url="https://www.sc.com/sg/faq"),
        lambda d: d["sources"][0].update(entity="交通银行股份有限公司香港分行"),
        lambda d: d["articles"][0].update(source_ids=["HSBC-HELP"]),
        lambda d: d["articles"].append(d["articles"][0]),
        lambda d: d["articles"][0].update(review_due_on="2026-10-01"),
        lambda d: d["sources"][0].update(verification="search_excerpt"),
    ],
)
def test_broken_or_wrong_provenance_configuration_fails_closed(client, settings, mutator):
    update(settings.bank_faq_path, mutator)
    assert client.get("/api/v1/health").json()["knowledge_base_status"] == "unavailable"
    result = client.get(f"/api/v1/sessions/{new(client)}/services")
    assert result.status_code == 503 and result.json()["error"]["code"] == "SERVICE_CONFIG_UNAVAILABLE"


def test_activation_duplicate_rejected(settings):
    update(
        settings.bank_faq_activation_path,
        lambda d: d["enabled_article_ids"].append(d["enabled_article_ids"][0]),
    )
    with pytest.raises(ServiceConfigUnavailable):
        faq_configuration(settings.bank_faq_path, settings.bank_faq_activation_path, [], [])


def test_changing_to_new_topic_without_bank_keeps_previous_bank(client):
    sid = new(client)
    ask(client, sid, "汇丰换手机怎样处理")
    answer = ask(client, sid, "开户需要什么材料")["answer"]
    assert answer["kind"] == "unknown" and answer["sources"] == []
    assert "香港上海汇丰银行" in answer["text"]
    assert "香港身份证" not in answer["text"]
    answer = ask(client, sid, "转数快收费吗")["answer"]
    assert ids(answer) == {"HKFAQ-HSBC-12"}


def test_two_banks_have_separate_clauses_and_provenance(client):
    answer = ask(client, new(client), "汇丰手机银行PIN忘了，恒生换手机怎样处理")["answer"]
    assert ids(answer) == {"HKFAQ-HSBC-05", "HKFAQ-HASE-07"}
    assert len(answer["parts"]) == 2
    assert "先移除旧设备" not in answer["parts"][0]["text"]


def test_known_and_unknown_topic_not_reported_as_fully_covered(client):
    answer = ask(client, new(client), "汇丰转数快有手续费吗，还有按揭申请条件")["answer"]
    assert "HKFAQ-HSBC-12" in ids(answer)
    assert answer["coverage"] == "partial"
    assert "另有未被本次来源覆盖的事项" in answer["text"]


def test_both_known_and_uncovered_bank_never_silently_omit_bank(client):
    answer = ask(client, new(client), "汇丰与花旗开户材料")["answer"]
    assert answer["kind"] == "unknown" and answer["sources"] == []


def test_hong_kong_general_fps_in_personal_and_enterprise(client):
    for kind in ("personal", "enterprise"):
        answer = ask(client, new(client, kind), "转数快是什么")["answer"]
        assert ids(answer) == {"HKFAQ-BOCOM-33"}
        assert "地区通用" in answer["text"]
        assert ids(ask(client, new(client, kind), "香港金管局转数快是什么")["answer"]) == {"HKFAQ-HKMA-09"}


def test_public_company_question_can_select_enterprise_without_personal_override(client):
    answer = ask(client, new(client), "交银香港企业转数快退款有什么期限")["answer"]
    assert ids(answer) == {"HKFAQ-BOCOM-10"}
    assert "企业服务" in answer["text"]


def test_old_topic_correction_does_not_keep_negative_intent(client):
    sid = new(client)
    ask(client, sid, "汇丰换手机怎样处理")
    answer = ask(client, sid, "不是换手机，是网银密码重置入口在哪里")["answer"]
    assert ids(answer) == {"HKFAQ-HSBC-03"}


def test_fps_wrong_destination_does_not_change_origin_bank(client):
    answer = ask(client, new(client), "汇丰香港SWIFT收内地银行汇款要什么资料")["answer"]
    assert "HKFAQ-HSBC-01" in ids(answer)
    assert "HSBCHKHHHKH" in answer["text"]


@pytest.mark.parametrize(
    "question,article",
    [
        ("渣打怎样注册 SC   Mobile  Key", "HKFAQ-SC-06"),
        ("渣打我忘了 Mobile Key PIN 该怎么办", "HKFAQ-SC-08"),
    ],
)
def test_english_product_name_spacing_uses_rules(client, question, article):
    assert ids(ask(client, new(client), question)["answer"]) == {article}


def test_faq_reading_starts_at_answered_fact_and_survives_refresh(client):
    sid = new(client)
    turn = ask(client, sid, "汇丰网银密码怎样重置")
    services = turn["services"]
    card = services["cards"][0]
    assert "密码重置" in card["steps"][card["step_index"]]
    restored = client.get(f"/api/v1/sessions/{sid}/services").json()["cards"][0]
    assert restored["step_index"] == card["step_index"]
    assert "密码重置" in restored["steps"][restored["step_index"]]


def test_open_api_revoke_synonyms_use_same_official_answer(client):
    answer = ask(client, new(client), "东亚开放API授权如何撤销")["answer"]
    assert ids(answer) == {"HKFAQ-BEA-09"}
    assert "本项目" in answer["text"]


LEGACY_ARTICLES = json.loads((ROOT / "docs/知识库/交通银行香港/knowledge-base.json").read_text())["articles"]


@pytest.mark.parametrize("article", LEGACY_ARTICLES, ids=[a["id"] for a in LEGACY_ARTICLES])
def test_original_thirty_questions_remain_source_bound_with_all_faqs_enabled(client, article):
    kind = "enterprise" if article["customer_type"] == "enterprise" else "personal"
    answer = ask(client, new(client, kind), article["questions"][0])["answer"]
    assert article["id"] in ids(answer)
    assert article["answer"] in answer["text"]
    assert article["limits"] in answer["text"]


@pytest.mark.parametrize(
    "question,forbidden",
    [
        ("交银香港转数快收费吗", "HKFAQ-HSBC-12"),
        ("交银香港SWIFT代码是什么", "HKFAQ-HSBC-01"),
        ("交银香港网购验证码多久过期", "HKFAQ-HSBC-14"),
    ],
)
def test_peer_specific_bank_terms_are_not_relabelled_as_bocom(client, question, forbidden):
    answer = ask(client, new(client), question)["answer"]
    assert forbidden not in ids(answer)
    assert "HSBCHKHHHKH" not in answer["text"]
    assert "100秒" not in answer["text"] and "3分钟" not in answer["text"]


def test_bocom_phone_change_uses_peer_guidance_without_importing_waiting_time(client):
    answer = ask(client, new(client), "交银香港换新手机要注意什么，等待多久")["answer"]
    assert "HKFAQ-BOCOM-31" in ids(answer)
    assert "同业通用参考" in answer["text"] and answer["coverage"] == "partial"
    assert "24小时" not in answer["text"] and "24 小时" not in answer["text"]
    assert all("非交银条款" in ref["locator"] for source in answer["sources"] for ref in source["references"])


def test_peer_guidance_with_bank_rate_or_missing_label_rejected(settings):
    update(
        settings.bank_faq_path,
        lambda d: next(a for a in d["articles"] if a["id"] == "HKFAQ-BOCOM-29").update(
            answer="建议：交银等待24小时即可重置"
        ),
    )
    with pytest.raises(ServiceConfigUnavailable):
        faq_configuration(settings.bank_faq_path, settings.bank_faq_activation_path, [], [])
