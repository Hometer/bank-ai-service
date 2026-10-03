"""Public corpus integration, coverage, source changes, and the existing safety boundaries."""

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge_base import knowledge_source, public_configuration
from app.main import create_app
from app.service import load_knowledge
from app.services_catalog import knowledge_fingerprint

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "docs/知识库/交通银行香港/knowledge-base.json"
ACTIVATION = ROOT / "backend/app/public_activation.json"
ARTICLES = json.loads(CORPUS.read_text())["articles"]


@pytest.fixture
def settings(tmp_path):
    corpus, activation = tmp_path / "corpus.json", tmp_path / "activation.json"
    shutil.copyfile(CORPUS, corpus)
    shutil.copyfile(ACTIVATION, activation)
    return Settings(database_url=f"sqlite:///{tmp_path / 'knowledge.sqlite3'}",
                    public_knowledge_path=corpus, public_activation_path=activation)


@pytest.fixture
def client(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("知识库优先链路不应发出模型请求")
    monkeypatch.setattr("app.consultation.model_route", forbidden)
    with TestClient(create_app(settings)) as current:
        yield current


def new(client, kind="personal"):
    response = client.post("/api/v1/sessions", json={"customer_type": kind})
    assert response.status_code == 201
    return response.json()["id"]


def ask(client, sid, text):
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text})
    assert response.status_code == 200, response.json()
    return response.json()


def article_ids(answer):
    return {aid for source in answer["sources"] for aid in source["article_ids"]}


def mutate(path, update):
    data = json.loads(path.read_text())
    update(data)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.replace(path)


@pytest.mark.parametrize("article", ARTICLES, ids=[a["id"] for a in ARTICLES])
def test_all_thirty_canonical_questions_use_sourced_knowledge(client, article):
    kind = "enterprise" if article["customer_type"] == "enterprise" else "personal"
    result = ask(client, new(client, kind), article["questions"][0])
    answer = result["answer"]
    assert article["id"] in article_ids(answer)
    assert article["answer"] in answer["text"]
    assert article["limits"] in answer["text"]
    assert answer["processing"] == "knowledge"
    assert "现有演示资料无法确认" not in answer["text"]
    for source in answer["sources"]:
        assert source["status"] == "public_unreviewed"
        assert source["retrieved_on"] == "2026-10-03"
        assert source["review_due_on"]
        assert source["references"]
        for ref in source["references"]:
            assert ref["url"].startswith("https://")
            assert ref["jurisdiction"] == "HK" and ref["locator"]


@pytest.mark.parametrize("kind,question,expected,missing", [
    ("personal", "只有护照能在App开户吗", "香港身份证", None),
    ("personal", "非永久居民想开户", "国籍证明", None),
    ("personal", "已有存款账户再全新开户", "网上银行或网点", None),
    ("personal", "App想开支票账户", "网点提交签名", None),
    ("personal", "普通港元定存最低金额", "10,000", None),
    ("personal", "今天港元定存利率是多少", "没有所问利率", "1.99%"),
    ("personal", "提前取定存会扣多少钱", "没有所问利率或收费", "2%"),
    ("personal", "通知存款是否固定到期", "没有固定到期日", None),
    ("personal", "浮动收入申请个人贷款", "三个月薪金", None),
    ("personal", "个人自雇贷款需要什么材料，企业融资也用这个清单吗", "不代表申请企业融资", None),
    ("personal", "DreamCash提前还一部分", "不接受部分提前清还", None),
    ("personal", "提款翌日开始冷静期", "七个历日内全数清还", None),
    ("personal", "按揭提前还款能套DreamCash费率吗", "不能套用到按揭", "2%"),
    ("enterprise", "香港有限公司全部开户材料", "附件未成功读取", None),
    ("enterprise", "初创多层股权公司简易开户", "不表示免核验或一定获批", None),
    ("enterprise", "简易企业账户收境外款", "不支持跨境汇入或汇出", None),
    ("enterprise", "企业对账单在哪里下载，需要什么权限", "认证及相应账户权限", "已经下载"),
    ("enterprise", "生成已盖章单笔付款回单", "不足以确认单笔付款回单", "已生成"),
    ("enterprise", "九成担保和八成期限相同吗", "没有所问", "2028"),
    ("enterprise", "担保或最低利率是否保证贷款获批", "不能保证获批", "1.99%"),
    ("personal", "每开一个账户都有80万元保障吗", "并非每个账户", None),
    ("personal", "基金保险结构性存款保障", "不在保障范围", None),
    ("enterprise", "远期外汇是否稳赚", "不能据此承诺盈利", None),
])
def test_research_acceptance_cases_01_to_23(client, kind, question, expected, missing):
    answer = ask(client, new(client, kind), question)["answer"]
    assert expected in answer["text"]
    if missing:
        assert missing not in answer["text"]


def test_research_case_24_all_records_past_review_are_unavailable(client, monkeypatch):
    monkeypatch.setattr("app.knowledge_base.business_date", lambda: "2026-11-03")
    assert client.get("/api/v1/health").json()["knowledge_base_status"] == "unavailable"
    result = ask(client, new(client), "开户材料")
    assert result["answer"]["sources"] == []
    assert "香港身份证" not in result["answer"]["text"]


def test_materials_fees_and_time_gaps_are_not_dropped(client):
    answer = ask(client, new(client), "个人开户材料和费用要多久")["answer"]
    assert "香港身份证" in answer["text"]
    assert "没有所问利率或收费" in answer["text"]
    assert "办理时限依据" in answer["text"]
    assert answer["coverage"] == "partial"


def test_shared_source_keeps_each_claim_locator(settings):
    _, items = public_configuration(settings.public_knowledge_path, settings.public_activation_path, [], [])
    item = next(item for item in items if item.id == "personal_opening")
    source = knowledge_source(item)
    for fact in item.public_facts:
        for ref in fact.references:
            assert ref.locator in next(current.locator for current in source.references if current.id == ref.id)


def test_explicit_new_income_category_overrides_context(client):
    sid = new(client)
    assert "BCHK-P11" in article_ids(ask(client, sid, "自雇贷款材料")["answer"])
    assert article_ids(ask(client, sid, "改为固定收入个人贷款材料")["answer"]) == {"BCHK-P09"}


def test_needed_permissions_are_not_document_requirements(client):
    answer = ask(client, new(client, "enterprise"), "企业对账单在哪里下载，需要什么权限")["answer"]
    assert {"BCHK-E04", "BCHK-E05"} <= article_ids(answer)
    assert "完整材料依据" not in answer["text"]
    assert "没有生成或下载结单" in answer["text"]


def test_public_flow_is_usable_even_with_empty_model_configuration(settings, monkeypatch):
    monkeypatch.setattr("app.consultation.model_route", lambda *a, **k: pytest.fail("must not call model"))
    with TestClient(create_app(replace(settings, mode="model"))) as client:
        assert ask(client, new(client), "开户材料")["answer"]["processing"] == "knowledge"


@pytest.mark.parametrize("question,expected", [
    ("查余额", "private_unavailable"), ("我的企业账单帮我查出来", "private_unavailable"),
    ("转人工", "human_unavailable"), ("验证码是123456", "sensitive_blocked"),
    ("银行卡被盗刷了", "urgent"),
])
def test_original_protected_paths_still_win(client, question, expected):
    sid = new(client)
    result = ask(client, sid, question)
    assert result["answer"]["kind"] == expected
    assert result["answer"]["sources"] == []
    if expected == "sensitive_blocked":
        assert "123456" not in client.get(f"/api/v1/sessions/{sid}").text


def test_private_and_public_parts_remain_separate(client):
    answer = ask(client, new(client), "个人查余额，企业开户材料")["answer"]
    assert answer["parts"][0]["kind"] == "private_unavailable"
    assert answer["parts"][0]["sources"] == []
    assert "BCHK-E01" in article_ids(answer)
    assert answer["processing"] == "knowledge"


def test_two_topics_clarify_correction_and_cancel(client):
    sid = new(client)
    result = ask(client, sid, "个人开户材料和企业开户材料")
    assert article_ids(result["answer"]) == {"BCHK-P02", "BCHK-E01"}
    assert ask(client, sid, "那需要什么")["answer"]["kind"] == "clarification"
    corrected = ask(client, sid, "不是开户，是个人存款")
    assert corrected["consultation"]["topic_ids"] == ["personal_deposit"]
    assert ask(client, sid, "取消")["consultation"]["status"] == "cancelled"
    assert ask(client, sid, "个人开户")["consultation"]["status"] == "idle"


def test_specific_income_category_is_kept_on_followup(client):
    sid = new(client)
    ask(client, sid, "自雇人士申请个人贷款")
    result = ask(client, sid, "那需要什么材料")
    assert "BCHK-P11" in article_ids(result["answer"])
    assert "BCHK-P09" not in article_ids(result["answer"])


@pytest.mark.parametrize("question", ["美元定存最低多少", "人民币定存最低多少", "大额定存最低多少"])
def test_general_hkd_minimum_is_not_used_for_other_products(client, question):
    answer = ask(client, new(client), question)["answer"]
    assert "10,000" not in answer["text"] and "BCHK-P06" not in article_ids(answer)


@pytest.mark.parametrize("question", ["汇丰开户材料", "交通银行台湾开户材料", "交通银行股份有限公司香港分行开户材料"])
def test_other_entities_do_not_borrow_hk_subsidiary_rules(client, question):
    answer = ask(client, new(client), question)["answer"]
    assert answer["kind"] == "unknown"
    assert answer["sources"] == [] and "不能混用" in answer["text"]


@pytest.mark.parametrize("change", [
    lambda x: x["articles"][0].update(limits=""),
    lambda x: x["articles"].append(x["articles"][0]),
    lambda x: x["articles"][0].update(source_ids=["NOT_A_SOURCE"]),
    lambda x: x["sources"][0].update(url="https://evil.example/rate"),
    lambda x: x["sources"][0].update(url="https://www.hk.bankcomm.com/hk/shtml/bhk/tw/a.shtml"),
    lambda x: x["articles"][0].update(jurisdiction="CN"),
    lambda x: x["articles"][0].update(review_due_on="2026-02-30"),
    lambda x: x["articles"][0].update(bank_reviewed=True),
])
def test_broken_public_corpus_fails_closed_but_safety_remains(client, settings, change):
    mutate(settings.public_knowledge_path, change)
    sid = new(client)
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"})
    assert response.status_code == 503
    assert set(response.json()) == {"error"}
    assert ask(client, sid, "银行卡被盗刷了")["answer"]["kind"] == "urgent"
    assert client.get("/api/v1/health").json()["knowledge_base_status"] == "unavailable"


def test_disabled_activation_does_not_restore_demo_as_public_fact(client, settings):
    mutate(settings.public_activation_path, lambda x: x.update(enabled_article_ids=[]))
    result = ask(client, new(client), "开户材料")
    assert result["answer"]["sources"] == []


def test_expired_fee_is_removed_while_other_loan_documents_remain(client, monkeypatch):
    monkeypatch.setattr("app.knowledge_base.business_date", lambda: "2026-10-11")
    answer = ask(client, new(client), "DreamCash提前还贷手续费")["answer"]
    assert "BCHK-P14" not in article_ids(answer) and "2%" not in answer["text"]
    assert "没有所问利率或收费" in answer["text"]
    assert "BCHK-P09" in article_ids(ask(client, new(client), "贷款材料")["answer"])


def test_changed_source_invalidates_old_navigation_and_history_card(client, settings):
    sid = new(client)
    first = ask(client, sid, "开户材料")
    mutate(settings.public_knowledge_path, lambda x: x["sources"][0].update(title="更新后的官网来源"))
    result = client.post(f"/api/v1/sessions/{sid}/navigation", json={"action": "next", "expected_revision": first["session_revision"]})
    assert result.status_code == 409
    history = client.get(f"/api/v1/sessions/{sid}").json()
    assert history["services"]["navigation"]["status"] == "invalid"
    assert history["messages"][-1]["answer"]["cards"][0]["valid"] is False


def test_source_changes_during_answer_are_not_persisted(client, settings, monkeypatch):
    from app import main
    original = main.answer_consultation

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        mutate(settings.public_knowledge_path, lambda x: x["articles"][1].update(answer="已更新公开内容"))
        return result
    monkeypatch.setattr(main, "answer_consultation", delayed)
    sid = new(client)
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"})
    assert response.status_code == 409
    assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_existing_demo_fingerprints_are_byte_compatible():
    raw = json.loads((ROOT / "backend/app/knowledge.json").read_text())
    items = load_knowledge()
    for original, item in zip(raw, items, strict=True):
        expected = hashlib.sha256(json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        assert knowledge_fingerprint(item) == expected


def test_new_instance_restores_answer_sources_and_subtopic(settings):
    with TestClient(create_app(settings)) as client:
        sid = new(client)
        first = ask(client, sid, "自雇人士申请个人贷款")
        owner = client.cookies.get("bank_demo_owner")
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set("bank_demo_owner", owner)
        history = restarted.get(f"/api/v1/sessions/{sid}").json()
        assert history["messages"][-1]["answer"]["sources"] == first["answer"]["sources"]
        assert "BCHK-P11" in article_ids(ask(restarted, sid, "那需要什么材料")["answer"])


def test_known_plus_unsupported_and_topic_limit(client):
    result = ask(client, new(client), "个人开户和信用卡积分兑换")
    assert result["answer"]["parts"][-1]["kind"] == "unknown"
    assert result["answer"]["parts"][-1]["sources"] == []
    assert "分批" in ask(client, new(client), "个人开户和存款和贷款")["answer"]["text"]
