"""V1.3 acceptance: guarded public navigation, live configuration, and retention."""

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Conversation, Message, cleanup_expired
from app.main import COOKIE, RETENTION, create_app

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def settings(tmp_path):
    catalog = tmp_path / "catalog.json"
    knowledge = tmp_path / "knowledge.json"
    shutil.copyfile(APP / "service_catalog.json", catalog)
    shutil.copyfile(APP / "knowledge.json", knowledge)
    return Settings(database_url=f"sqlite:///{tmp_path / 'v13.sqlite3'}",
                    catalog_path=catalog, knowledge_path=knowledge)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as current:
        yield current


def new_session(client, customer_type="personal"):
    response = client.post("/api/v1/sessions", json={"customer_type": customer_type})
    assert response.status_code == 201
    return response.json()["id"]


def services(client, sid):
    response = client.get(f"/api/v1/sessions/{sid}/services")
    assert response.status_code == 200
    return response.json()


def action(client, sid, name, view=None, item=None):
    view = view or services(client, sid)
    body = {"action": name, "expected_revision": view["session_revision"]}
    if item is not None:
        body.update(capability_id=item["capability_id"], knowledge_version=item["knowledge_version"])
    return client.post(f"/api/v1/sessions/{sid}/navigation", json=body)


def start(client, sid, index=0):
    view = services(client, sid)
    response = action(client, sid, "start", view, view["catalog"][index])
    assert response.status_code == 200
    return response.json()


def send(client, sid, text, **extra):
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text, **extra})
    assert response.status_code == 200
    return response.json()


def rewrite(path, change):
    items = json.loads(path.read_text())
    change(items)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def disable_capability(settings, capability_id):
    rewrite(settings.catalog_path, lambda items: [item.update(enabled=False) for item in items
                                                 if item["capability_id"] == capability_id])


def assert_error(response, status, code):
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("customer_type,expected", [
    ("personal", {"personal_opening", "personal_deposit", "personal_repayment"}),
    ("enterprise", {"enterprise_opening", "enterprise_reconciliation", "enterprise_financing"}),
])
def test_catalog_only_exposes_three_public_topics_for_current_object(client, customer_type, expected):
    view = services(client, new_session(client, customer_type))
    assert {item["knowledge_id"] for item in view["catalog"]} == expected
    assert all(item["customer_type"] == customer_type and item["status"] == "demo_available"
               for item in view["catalog"])
    assert view["cards"] == [] and view["navigation"]["status"] == "idle"
    assert "http" not in json.dumps(view)
    assert "热门" not in json.dumps(view, ensure_ascii=False)


def test_foreign_object_capability_is_rejected_but_explicit_public_question_still_works(client):
    personal = new_session(client)
    enterprise = services(client, new_session(client, "enterprise"))["catalog"][0]
    assert_error(action(client, personal, "start", item=enterprise), 409, "CAPABILITY_CHANGED")
    result = send(client, personal, "企业开户材料")
    assert result["answer"]["sources"][0]["id"] == "enterprise_opening"
    assert result["answer"]["cards"] == []
    assert result["services"]["navigation"]["status"] != "reading"
    assert result["services"]["suggestions"] == []


def test_mixed_private_and_public_question_keeps_separate_authority_and_no_private_cards(client):
    sid = new_session(client)
    result = send(client, sid, "个人查余额，企业开户材料")
    parts = {part["customer_type"]: part for part in result["answer"]["parts"]}
    assert parts["personal"]["kind"] == "private_unavailable"
    assert parts["personal"]["sources"] == []
    assert parts["enterprise"]["kind"] == "answered"
    assert parts["enterprise"]["sources"][0]["id"] == "enterprise_opening"
    assert result["answer"]["cards"] == []
    assert result["services"]["cards"] == []


def test_public_cards_have_exact_source_metadata_and_read_only_steps(client, settings):
    sid = new_session(client)
    view = start(client, sid)
    records = {item["id"]: item for item in json.loads(settings.knowledge_path.read_text())}
    assert view["navigation"]["status"] == "reading"
    assert view["cards"]
    for card in view["cards"]:
        source = card["source"]
        knowledge = records[source["id"]]
        assert card["kind"] in {"public_info", "reading_steps"}
        assert card["valid"] and card["status"] == "demo_available"
        assert card["message_id"] is not None and card["customer_type"] == "personal"
        for field in ("title", "version", "valid_from", "valid_until", "status"):
            assert source[field] == knowledge[field]
        assert card["text"] == knowledge["text"]
        assert card["steps"] and all(step in knowledge["text"] for step in card["steps"])
        assert set(card["actions"]) <= {"start", "next", "back", "cancel"}
        assert "url" not in card and "balance" not in card


def test_suggestions_are_bounded_same_topic_and_use_guarded_message_entry(client):
    sid = new_session(client)
    view = start(client, sid)
    assert 1 <= len(view["suggestions"]) <= 3
    for item in view["suggestions"]:
        assert item["knowledge_id"] == view["navigation"]["knowledge_id"]
        assert item["customer_type"] == "personal"
    choice = view["suggestions"][0]
    result = send(client, sid, choice["question"], capability_id=choice["capability_id"],
                  knowledge_version=choice["knowledge_version"])
    assert result["answer"]["sources"][0]["id"] == choice["knowledge_id"]
    assert len(result["services"]["suggestions"]) <= 3


def test_next_back_cancel_and_explicit_restart_have_durable_semantics(client):
    sid = new_session(client)
    view = start(client, sid)
    assert view["navigation"]["step_index"] == 0
    assert_error(action(client, sid, "back", view), 409, "NAVIGATION_UNAVAILABLE")
    forward = action(client, sid, "next", view)
    assert forward.status_code == 200 and forward.json()["navigation"]["step_index"] == 1
    back = action(client, sid, "back", forward.json())
    assert back.status_code == 200 and back.json()["navigation"]["step_index"] == 0
    cancelled = action(client, sid, "cancel", back.json())
    assert cancelled.status_code == 200
    assert services(client, sid)["navigation"]["status"] == "cancelled"
    assert services(client, sid)["cards"] == [] and services(client, sid)["suggestions"] == []
    assert_error(action(client, sid, "next"), 409, "NAVIGATION_UNAVAILABLE")
    assert start(client, sid)["navigation"]["status"] == "reading"


@pytest.mark.parametrize("text,kind", [
    ("查余额", "private_unavailable"), ("转人工", "human_unavailable"),
    ("没有覆盖的业务", "unknown"), ("验证码是123456", "sensitive_blocked"),
    ("银行卡被盗刷了", "urgent"),
])
def test_non_public_routes_never_create_private_sample_cards(client, text, kind):
    sid = new_session(client)
    start(client, sid)
    result = send(client, sid, text)
    assert result["answer"]["kind"] == kind and result["answer"]["cards"] == []
    view = services(client, sid)
    assert view["navigation"]["status"] != "reading" and view["cards"] == []
    assert "123456" not in json.dumps(client.get(f"/api/v1/sessions/{sid}").json())


def test_cancel_does_not_dismiss_emergency_notice(client):
    sid = new_session(client)
    send(client, sid, "疑似诈骗")
    start(client, sid)
    assert action(client, sid, "cancel").status_code == 200
    assert client.get("/api/v1/safety").json()["active"]


@pytest.mark.parametrize("mutation", ["disabled", "version", "expired", "empty", "missing"])
def test_live_knowledge_change_invalidates_old_cards_and_actions(client, settings, mutation):
    sid = new_session(client)
    view = start(client, sid)
    knowledge_id = view["navigation"]["knowledge_id"]

    def change(items):
        for item in items[:]:
            if item["id"] == knowledge_id:
                if mutation == "missing":
                    items.remove(item)
                else:
                    item.update({"disabled": {"status": "disabled"}, "version": {"version": "demo-v2"},
                                 "expired": {"valid_until": "2000-01-01"}, "empty": {"text": ""}}[mutation])

    rewrite(settings.knowledge_path, change)
    current = services(client, sid)
    assert all(item["knowledge_id"] != knowledge_id for item in current["catalog"] + current["suggestions"])
    assert current["navigation"]["status"] == "invalid"
    assert all(not card["valid"] and not card["actions"] for card in current["cards"])
    assert_error(action(client, sid, "next", view), 409, "CAPABILITY_CHANGED")
    history = client.get(f"/api/v1/sessions/{sid}").json()
    old_cards = [card for message in history["messages"] if message["answer"]
                 for card in message["answer"]["cards"]]
    assert old_cards and all(not card["valid"] and not card["actions"] for card in old_cards)


def test_real_catalog_file_switch_blocks_captured_start_suggestion_and_history(client, settings):
    sid = new_session(client)
    view = start(client, sid)
    choice = view["suggestions"][0]
    disable_capability(settings, choice["capability_id"])
    current = services(client, sid)
    assert current["navigation"]["status"] == "invalid"
    assert all(item["capability_id"] != choice["capability_id"] for item in current["catalog"])
    assert_error(action(client, sid, "start", current, choice), 409, "CAPABILITY_CHANGED")
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={
        "text": choice["question"], "capability_id": choice["capability_id"],
        "knowledge_version": choice["knowledge_version"],
    })
    assert_error(response, 409, "CAPABILITY_CHANGED")
    assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2
    assert send(client, sid, "疑似诈骗")["safety"]["active"]


def test_same_version_content_change_invalidates_saved_navigation_until_explicit_restart(client, settings):
    sid = new_session(client)
    view = start(client, sid)
    knowledge_id = view["navigation"]["knowledge_id"]
    new_text = "演示说明：这是一份已更新的公开说明。本演示仍不能确认目标银行的具体要求。"
    rewrite(settings.knowledge_path, lambda items: [item.update(text=new_text) for item in items
                                                   if item["id"] == knowledge_id])
    current = services(client, sid)
    assert current["navigation"]["status"] == "invalid" and current["cards"] == []
    assert current["session_revision"] == view["session_revision"]
    for operation in ("next", "back"):
        assert_error(action(client, sid, operation, current), 409, "CAPABILITY_CHANGED")
    unchanged = services(client, sid)
    assert unchanged["session_revision"] == view["session_revision"]
    history = client.get(f"/api/v1/sessions/{sid}").json()
    old_cards = [card for message in history["messages"] if message["answer"]
                 for card in message["answer"]["cards"]]
    assert old_cards and all(not card["valid"] and card["actions"] == [] for card in old_cards)
    assert action(client, sid, "cancel", unchanged).status_code == 200
    assert services(client, sid)["navigation"]["status"] == "cancelled"
    refreshed = start(client, sid)
    assert refreshed["navigation"]["status"] == "reading"
    assert refreshed["cards"] and all(card["valid"] and card["text"] == new_text
                                       for card in refreshed["cards"])


def test_invalid_configuration_preserves_urgent_redaction_and_cancel(client, settings):
    sid = new_session(client)
    view = start(client, sid)
    settings.catalog_path.write_text("{broken", encoding="utf-8")
    assert_error(client.get(f"/api/v1/sessions/{sid}/services"), 503, "SERVICE_CONFIG_UNAVAILABLE")
    cancelled = action(client, sid, "cancel", view)
    assert cancelled.status_code == 200 and cancelled.json()["navigation"]["status"] == "cancelled"
    assert send(client, sid, "验证码是123456")["answer"]["kind"] == "sensitive_blocked"
    assert send(client, sid, "银行卡被盗刷了")["safety"]["active"]
    assert send(client, sid, "转人工")["answer"]["kind"] == "human_unavailable"


@pytest.mark.parametrize("body", [
    {"action": "pay", "expected_revision": 0},
    {"action": "cancel", "expected_revision": True},
    {"action": "cancel", "expected_revision": "0"},
    {"action": "cancel", "expected_revision": -1},
    {"action": "start", "expected_revision": 0},
    {"action": "next", "expected_revision": 0, "capability_id": "personal_opening", "knowledge_version": "demo-v1"},
    {"action": "cancel", "expected_revision": 0, "url": "https://invalid.example/pay"},
])
def test_navigation_rejects_unknown_actions_and_untrusted_parameters(client, body):
    sid = new_session(client)
    assert_error(client.post(f"/api/v1/sessions/{sid}/navigation", json=body), 422, "INVALID_INPUT")
    assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_unknown_capability_or_unconfigured_suggestion_does_not_execute(client):
    sid = new_session(client)
    view = services(client, sid)
    invented = {"capability_id": "https://invalid.example/pay", "knowledge_version": "demo-v1"}
    assert_error(action(client, sid, "start", view, invented), 409, "CAPABILITY_CHANGED")
    choice = view["catalog"][0]
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={
        "text": "查余额并转账", "capability_id": choice["capability_id"],
        "knowledge_version": choice["knowledge_version"],
    })
    assert response.status_code in (409, 422)
    assert "error" in response.json()
    assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_service_and_navigation_owner_isolation(settings):
    with TestClient(create_app(settings)) as first, TestClient(create_app(settings)) as other:
        sid = new_session(first)
        view = start(first, sid)
        assert other.get(f"/api/v1/sessions/{sid}/services").status_code == 401
        new_session(other)
        assert other.get(f"/api/v1/sessions/{sid}/services").status_code == 404
        assert action(other, sid, "cancel", view).status_code == 404
        assert services(first, sid)["navigation"]["status"] == "reading"


def test_stale_revision_cannot_cancel_or_advance_newer_navigation(client):
    sid = new_session(client)
    old = start(client, sid)
    current = action(client, sid, "next", old)
    assert current.status_code == 200
    assert_error(action(client, sid, "cancel", old), 409, "CONTEXT_CHANGED")
    assert services(client, sid)["navigation"]["step_index"] == 1


def test_old_message_arriving_after_cancel_is_rejected_but_urgent_input_still_routes(client):
    sid = new_session(client)
    old = start(client, sid)
    assert action(client, sid, "cancel", old).status_code == 200
    stale = {"text": "开户材料", "expected_revision": old["session_revision"]}
    assert_error(client.post(f"/api/v1/sessions/{sid}/messages", json=stale), 409, "CONTEXT_CHANGED")
    assert services(client, sid)["navigation"]["status"] == "cancelled"
    urgent = send(client, sid, "银行卡被盗刷了", expected_revision=old["session_revision"])
    assert urgent["answer"]["kind"] == "urgent" and urgent["safety"]["active"]
    assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 4


def test_cancel_during_ordinary_response_prevents_late_navigation_restore(client, monkeypatch):
    import app.main as main

    sid = new_session(client)
    view = start(client, sid)
    original = main.answer_consultation
    entered, release = threading.Event(), threading.Event()

    def delayed(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(main, "answer_consultation", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/sessions/{sid}/messages", json={"text": "开户材料"})
        assert entered.wait(5)
        cancelled = action(client, sid, "cancel", view)
        release.set()
        assert cancelled.status_code == 200
        assert_error(pending.result(5), 409, "CONTEXT_CHANGED")
    assert services(client, sid)["navigation"]["status"] == "cancelled"
    assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2


def test_capability_closed_while_answer_is_computed_never_persists_old_conclusion(client, settings, monkeypatch):
    import app.main as main

    sid = new_session(client)
    choice = services(client, sid)["catalog"][0]
    original = main.answer_consultation

    def close_after_computing(*args):
        result = original(*args)
        disable_capability(settings, choice["capability_id"])
        return result

    monkeypatch.setattr(main, "answer_consultation", close_after_computing)
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": choice["question"]})
    assert_error(response, 409, "CAPABILITY_CHANGED")
    assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_navigation_restarts_preserve_progress_and_cancel_without_extending_retention(client, settings):
    sid = new_session(client)
    before = client.get(f"/api/v1/sessions/{sid}").json()["session"]
    view = start(client, sid)
    assert action(client, sid, "next", view).status_code == 200
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restored = services(restarted, sid)
        assert restored["navigation"]["status"] == "reading"
        assert restored["navigation"]["step_index"] == 1
        assert restored["cards"]
        assert action(restarted, sid, "cancel", restored).status_code == 200
    with TestClient(create_app(settings)) as again:
        again.cookies.update(client.cookies)
        assert services(again, sid)["navigation"]["status"] == "cancelled"
        after = again.get(f"/api/v1/sessions/{sid}").json()["session"]
        assert before["created_at"] == after["created_at"]
        assert before["expires_at"] == after["expires_at"]
    with client.app.state.sessions() as db:
        record = db.get(Conversation, sid)
        assert record.expires_at - record.created_at == RETENTION


def test_expiry_denies_actions_and_deletes_cards_and_navigation_with_session(client):
    sid = new_session(client)
    view = start(client, sid)
    with client.app.state.sessions.begin() as db:
        db.get(Conversation, sid).expires_at = time.time() - 1
    assert client.get(f"/api/v1/sessions/{sid}/services").status_code == 404
    assert action(client, sid, "next", view).status_code == 404
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert cleanup_expired(client.app.state.engine) == 1
    with client.app.state.sessions() as db:
        assert db.get(Conversation, sid) is None
        assert list(db.scalars(select(Message).where(Message.session_id == sid))) == []


def test_session_expiring_during_start_does_not_save_cards_or_navigation(client, monkeypatch):
    import app.main as main

    sid = new_session(client)
    view = services(client, sid)
    with client.app.state.sessions() as db:
        expiry = db.get(Conversation, sid).expires_at
    clock = [time.time()]
    original = main.resolve_service

    def cross_deadline(*args):
        result = original(*args)
        clock[0] = expiry + 1
        return result

    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "resolve_service", cross_deadline)
    assert_error(action(client, sid, "start", view, view["catalog"][0]), 404, "NOT_FOUND")
    with client.app.state.sessions() as db:
        assert list(db.scalars(select(Message).where(Message.session_id == sid))) == []
        assert db.get(Conversation, sid).revision == view["session_revision"]


def test_0002_history_without_new_json_fields_remains_readable_after_additive_migration(tmp_path):
    database = tmp_path / "old-v12.sqlite3"
    token = "local-compatibility-owner"
    now = time.time()
    expiry = now + 3600
    old_answer = {"kind": "answered", "text": "旧公开演示答复", "mode": "demo", "sources": []}
    with sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0002');
            CREATE TABLE conversations (id VARCHAR PRIMARY KEY, owner_hash VARCHAR NOT NULL,
              customer_type VARCHAR NOT NULL, created_at FLOAT NOT NULL, expires_at FLOAT NOT NULL,
              context JSON NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id VARCHAR NOT NULL,
              role VARCHAR NOT NULL, text VARCHAR NOT NULL, created_at FLOAT NOT NULL, answer JSON,
              FOREIGN KEY(session_id) REFERENCES conversations(id) ON DELETE CASCADE);
            CREATE TABLE safety_notices (owner_hash VARCHAR PRIMARY KEY, active BOOLEAN NOT NULL,
              revision INTEGER NOT NULL, updated_at FLOAT NOT NULL, expires_at FLOAT NOT NULL);
        ''')
        db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                   ("old", hashlib.sha256(token.encode()).hexdigest(), "personal", now, expiry,
                    json.dumps({"topics": [], "clarification_attempts": 0}), 2))
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (1, "old", "assistant", old_answer["text"], now, json.dumps(old_answer)))
    with TestClient(create_app(Settings(database_url=f"sqlite:///{database}"))) as migrated:
        migrated.cookies.set(COOKIE, token)
        history = migrated.get("/api/v1/sessions/old")
        assert history.status_code == 200
        assert history.json()["messages"][0]["answer"]["cards"] == []
        assert history.json()["messages"][0]["text"] == old_answer["text"]
        assert services(migrated, "old")["navigation"]["status"] == "idle"
        assert start(migrated, "old")["navigation"]["status"] == "reading"
        with migrated.app.state.sessions() as db:
            record = db.get(Conversation, "old")
            assert record.created_at == now and record.expires_at == expiry
            assert db.get(Message, 1).answer == old_answer
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"
