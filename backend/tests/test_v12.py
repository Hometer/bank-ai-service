"""V1.2 behavior checks, including negative counterparts and persisted state."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.consultation import answer_consultation
from app.db import Conversation, Message, SafetyNotice, cleanup_expired
from app.main import create_app
from app.schemas import ConsultationContext, TopicRef
from app.service import BLOCKED_TEXT, load_knowledge, sanitize


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'v12.sqlite3'}")
    with TestClient(create_app(settings)) as client:
        yield client


def new_session(client, customer_type="personal"):
    response = client.post("/api/v1/sessions", json={"customer_type": customer_type})
    assert response.status_code == 201
    return response.json()["id"]


def send(client, sid, text):
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("text", [
    "忘记密码怎么办", "验证码收不到", "验证码收不到怎么办", "密码重置流程",
    "2026年资料哪里查", "贷款期限36个月", "借款10000元如何还款", "2026-10-02网点在哪里",
    "贷款利率3.5%是什么意思", "验证码是多少位", "验证码是什么",
])
def test_public_questions_are_not_credentials(client, text):
    result = send(client, new_session(client), text)
    assert result["user_text"] == text
    assert result["answer"]["kind"] != "sensitive_blocked"


@pytest.mark.parametrize("text", [
    "验证码是123456", "验证码：１２３４５６", "我的交易密码是春暖花开",
    "密码：letmein", "我的密码abcDEF!", "API key = fake-demo-only-value",
    "银行卡 6222 3333 4444 5555", "手机号是13800000000", "身份证是11010120000101001X",
    "演示123456", "1234", "user@example.test", "验证码是1234元",
])
def test_disclosure_is_masked_before_history_and_model(client, text, monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("A secret cannot reach model selection")

    monkeypatch.setattr("app.service.model_select", reject)
    sid = new_session(client)
    result = send(client, sid, text)
    assert result["user_text"] == BLOCKED_TEXT
    assert result["answer"]["kind"] == "sensitive_blocked"
    assert text not in json.dumps(client.get(f"/api/v1/sessions/{sid}").json(), ensure_ascii=False)
    _, answer, context = answer_consultation(text, "personal", load_knowledge(), Settings(mode="model"),
                                            ConsultationContext())
    assert answer.kind == "sensitive_blocked" and context.topics == []


def test_cross_scope_answer_is_public_and_labeled_without_changing_session(client):
    sid = new_session(client)
    result = send(client, sid, "企业开户材料")
    answer = result["answer"]
    assert answer["parts"][0]["customer_type"] == "enterprise"
    assert answer["sources"][0]["id"] == "enterprise_opening"
    assert "企业公开咨询" in answer["text"]
    assert client.get(f"/api/v1/sessions/{sid}").json()["session"]["customer_type"] == "personal"
    followup = send(client, sid, "那需要什么")
    assert followup["answer"]["sources"][0]["id"] == "enterprise_opening"


@pytest.mark.parametrize("question,expected", [
    ("个人贷款和企业融资都要哪些材料", ["personal_repayment", "enterprise_financing"]),
    ("个人和企业开户材料", ["personal_opening", "enterprise_opening"]),
])
def test_mixed_objects_have_independent_sources(client, question, expected):
    sid = new_session(client)
    answer = send(client, sid, question)["answer"]
    assert [source["id"] for source in answer["sources"]] == expected
    assert {part["customer_type"] for part in answer["parts"]} == {"personal", "enterprise"}
    followup = send(client, sid, "那需要什么")
    assert followup["answer"]["kind"] == "clarification"
    assert followup["answer"]["sources"] == []


def test_unknown_object_fragment_does_not_borrow_other_topic(client):
    answer = send(client, new_session(client), "个人信用卡和企业贷款材料")["answer"]
    assert answer["parts"][0]["kind"] == "unknown"
    assert answer["parts"][0]["sources"] == []
    assert answer["parts"][1]["sources"][0]["id"] == "enterprise_financing"


def test_topic_followup_and_cancel_are_persisted(client):
    sid = new_session(client)
    send(client, sid, "开户材料")
    result = send(client, sid, "那需要什么")
    assert result["answer"]["sources"][0]["id"] == "personal_opening"
    send(client, sid, "取消")
    assert send(client, sid, "那需要什么")["answer"]["kind"] == "clarification"
    # V1.4 emits the second clarification before exhausting on the next vague reply.
    assert send(client, sid, "那需要什么")["answer"]["kind"] == "clarification"
    assert send(client, sid, "那需要什么")["answer"]["kind"] == "unknown"
    assert send(client, sid, "开户材料")["answer"]["kind"] == "answered"
    with client.app.state.sessions() as db:
        assert db.get(Conversation, sid).context["clarification_attempts"] == 0


def test_unknown_and_private_question_do_not_reuse_stale_topic(client):
    sid = new_session(client)
    send(client, sid, "开户材料")
    send(client, sid, "我想了解没覆盖的业务")
    assert send(client, sid, "那需要什么")["answer"]["kind"] == "clarification"
    send(client, sid, "开户材料")
    assert send(client, sid, "查余额")["answer"]["kind"] == "private_unavailable"
    assert send(client, sid, "那需要什么")["answer"]["kind"] == "clarification"


def test_ambiguous_topics_and_expired_knowledge_are_not_guessed(client):
    sid = new_session(client)
    # V1.4 answers two explicit topics with separate sources instead of treating them as vague.
    mixed = send(client, sid, "个人开户和存款材料")["answer"]
    assert mixed["kind"] == "answered"
    assert [part["sources"][0]["id"] for part in mixed["parts"]] == ["personal_opening", "personal_deposit"]
    send(client, sid, "开户材料")
    client.app.state.knowledge = [item.model_copy(update={"status": "disabled"})
                                for item in client.app.state.knowledge]
    answer = send(client, sid, "那需要什么")["answer"]
    assert answer["kind"] == "unknown" and answer["sources"] == []


def test_explicit_new_object_never_inherits_old_topic(client):
    sid = new_session(client)
    send(client, sid, "个人开户材料")
    assert send(client, sid, "企业呢")["answer"]["kind"] == "clarification"


def test_safety_survives_normal_answers_switch_and_explicit_dismiss(client):
    sid = new_session(client)
    urgent = send(client, sid, "银行卡被盗刷，验证码是123456")
    assert urgent["safety"]["active"]
    assert "123456" not in json.dumps(urgent)
    revision = urgent["safety"]["revision"]
    normal = send(client, sid, "开户材料")
    assert normal["safety"]["active"] and normal["safety"]["revision"] == revision
    enterprise = new_session(client, "enterprise")
    assert send(client, enterprise, "开户材料")["safety"]["active"]
    closed = client.post("/api/v1/safety/dismiss", json={"expected_revision": revision})
    assert closed.status_code == 200 and not closed.json()["active"]
    assert not client.get("/api/v1/safety").json()["active"]
    assert not send(client, sid, "开户材料")["safety"]["active"]
    again = send(client, sid, "疑似诈骗")
    assert again["safety"]["active"] and again["safety"]["revision"] > revision
    stale = client.post("/api/v1/safety/dismiss", json={"expected_revision": revision})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "SAFETY_CHANGED"
    assert client.get("/api/v1/safety").json()["active"]


def test_safety_requires_owner_and_valid_request(client):
    assert client.get("/api/v1/safety").status_code == 401
    assert client.post("/api/v1/safety/dismiss", json={"expected_revision": 0}).status_code == 401
    new_session(client)
    for value in (-1, True, "1"):
        assert client.post("/api/v1/safety/dismiss", json={"expected_revision": value}).status_code == 422


def test_safety_owner_isolation_and_expiry(client):
    sid = new_session(client)
    send(client, sid, "银行卡被盗刷")
    settings = Settings(database_url=client.app.state.engine.url.render_as_string())
    with TestClient(create_app(settings)) as other:
        new_session(other)
        assert not other.get("/api/v1/safety").json()["active"]
    with client.app.state.sessions.begin() as db:
        state = db.scalars(select(SafetyNotice)).first()
        assert state.expires_at == db.get(Conversation, sid).expires_at
        state.expires_at = time.time() - 1
    assert not client.get("/api/v1/safety").json()["active"]
    cleanup_expired(client.app.state.engine)
    with client.app.state.sessions() as db:
        assert db.scalars(select(SafetyNotice)).first() is None


def test_pending_old_turn_cannot_overwrite_new_context(client, monkeypatch):
    import app.main as main

    sid = new_session(client)
    entered, release = threading.Event(), threading.Event()
    original = main.answer_consultation

    def delayed(text, *args):
        if text == "开户材料":
            entered.set()
            assert release.wait(5)
        return original(text, *args)

    monkeypatch.setattr(main, "answer_consultation", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"})
        assert entered.wait(5)
        send(client, sid, "存款规则")
        release.set()
        response = pending.result(5)
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONTEXT_CHANGED"
    with client.app.state.sessions() as db:
        assert db.get(Conversation, sid).context["topics"][0]["knowledge_id"] == "personal_deposit"
        assert len(list(db.scalars(select(Message).where(Message.session_id == sid)))) == 2


def test_session_expired_during_response_is_not_saved(client, monkeypatch):
    import app.main as main

    sid = new_session(client)
    original = main.answer_consultation

    def expire(*args):
        answer = original(*args)
        with client.app.state.sessions.begin() as db:
            db.get(Conversation, sid).expires_at = time.time() - 1
        return answer

    monkeypatch.setattr(main, "answer_consultation", expire)
    assert client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"}).status_code == 404
    with client.app.state.sessions() as db:
        assert list(db.scalars(select(Message).where(Message.session_id == sid))) == []


def test_concurrent_ordinary_turn_does_not_suppress_urgent_notice(client, monkeypatch):
    import app.main as main

    sid = new_session(client)
    entered, release = threading.Event(), threading.Event()
    original = main.answer_consultation

    def delayed(text, *args):
        if text == "疑似诈骗":
            entered.set()
            assert release.wait(5)
        return original(text, *args)

    monkeypatch.setattr(main, "answer_consultation", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/sessions/{sid}/messages", json={"text": "疑似诈骗"})
        assert entered.wait(5)
        send(client, sid, "开户材料")
        release.set()
        response = pending.result(5)
    assert response.status_code == 200 and response.json()["safety"]["active"]
    with client.app.state.sessions() as db:
        assert db.get(Conversation, sid).context["topics"] == []
        assert len(list(db.scalars(select(Message).where(Message.session_id == sid)))) == 4


def test_context_and_closed_safety_restore_in_new_app(client):
    sid = new_session(client)
    send(client, sid, "银行卡被盗刷")
    send(client, sid, "开户材料")
    settings = Settings(database_url=client.app.state.engine.url.render_as_string())
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/api/v1/safety").json()["active"]
        assert send(restarted, sid, "那需要什么")["answer"]["sources"][0]["id"] == "personal_opening"
        state = restarted.get("/api/v1/safety").json()
        restarted.post("/api/v1/safety/dismiss", json={"expected_revision": state["revision"]})
    with TestClient(create_app(settings)) as again:
        again.cookies.update(client.cookies)
        assert not again.get("/api/v1/safety").json()["active"]


def test_additive_migration_preserves_0001_history(tmp_path):
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0001');
            CREATE TABLE conversations (id VARCHAR PRIMARY KEY, owner_hash VARCHAR NOT NULL,
              customer_type VARCHAR NOT NULL, created_at FLOAT NOT NULL, expires_at FLOAT NOT NULL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id VARCHAR NOT NULL,
              role VARCHAR NOT NULL, text VARCHAR NOT NULL, created_at FLOAT NOT NULL, answer JSON,
              FOREIGN KEY(session_id) REFERENCES conversations(id) ON DELETE CASCADE);
        ''')
        db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("old", "owner", "personal", time.time(), time.time()+3600))
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (1, "old", "user", "旧演示消息", time.time(), None))
    with TestClient(create_app(Settings(database_url=f"sqlite:///{database}"))) as migrated:
        with migrated.app.state.sessions() as db:
            old = db.get(Conversation, "old")
            assert old.context == {} and old.revision == 0
            assert db.get(Message, 1).text == "旧演示消息"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"


def test_expired_context_reference_is_never_used():
    context = ConsultationContext(topics=[TopicRef(knowledge_id="absent", customer_type="personal")])
    _, answer, following = answer_consultation("那需要什么", "personal", [], Settings(), context)
    assert answer.kind == "unknown" and following.topics == []


def test_plain_year_number_is_not_automatically_a_date():
    assert sanitize("20261002") == BLOCKED_TEXT
    assert sanitize("2026年") == "2026年"
