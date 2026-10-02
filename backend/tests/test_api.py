import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Conversation, Message, cleanup_expired
from app.main import COOKIE, RETENTION, create_app
from app.service import BLOCKED_TEXT


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'demo.sqlite3'}")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as current:
        yield current


def create(client, customer_type="personal"):
    response = client.post("/api/v1/sessions", json={"customer_type": customer_type})
    assert response.status_code == 201
    return response.json()["id"]


def ask(client, text, customer_type="personal"):
    sid = create(client, customer_type)
    result = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text})
    assert result.status_code == 200
    return result.json()


@pytest.mark.parametrize("kind,text", [
    ("answered", "开户需要什么材料"),
    ("unknown", "今年利率是多少"),
    ("private_unavailable", "查余额"),
    ("urgent", "银行卡被盗刷了，请查余额"),
    ("urgent", "未登录，我的企业凭证被冒用了"),
    ("human_unavailable", "转人工"),
    ("human_unavailable", "投诉扣费"),
])
def test_routes_have_honest_states(client, kind, text):
    answer = ask(client, text)["answer"]
    assert answer["kind"] == kind
    assert answer["mode"] == "demo"
    assert "尚未配置" in answer["channel_status"]
    assert "已挂失" not in answer["text"]
    assert "已连接" not in answer["text"]


def test_customer_knowledge_is_separated(client):
    personal = ask(client, "开户材料")["answer"]
    enterprise = ask(client, "开户材料", "enterprise")["answer"]
    assert personal["sources"][0]["id"] == "personal_opening"
    assert enterprise["sources"][0]["id"] == "enterprise_opening"
    assert all(source["status"] == "demo_unreviewed" for source in enterprise["sources"])


@pytest.mark.parametrize("text", ["验证码是123456", "密码：letmein", "我的密码abcDEF!", "API key = fake-demo-only-value", "user@example.test", "银行卡 6222 3333 4444 5555"])
def test_sensitive_input_never_persists(client, text):
    sid = create(client)
    response = client.post(f"/api/v1/sessions/{sid}/messages", json={"text": text})
    assert response.json()["user_text"] == BLOCKED_TEXT
    assert response.json()["answer"]["kind"] == "sensitive_blocked"
    saved = client.get(f"/api/v1/sessions/{sid}").json()
    assert text not in json.dumps(saved, ensure_ascii=False)
    with client.app.state.sessions() as db:
        assert db.scalars(select(Message).where(Message.session_id == sid)).first().text == BLOCKED_TEXT


def test_urgent_input_with_secret_keeps_urgent_path_and_masks(client):
    result = ask(client, "银行卡被盗刷，验证码是123456")
    assert result["answer"]["kind"] == "urgent"
    assert result["user_text"] == BLOCKED_TEXT
    assert "123456" not in json.dumps(result)


@pytest.mark.parametrize("body", [{"text": ""}, {"text": " "}, {"text": "x" * 2001}, {"text": "开户", "tool": "pay"}])
def test_invalid_request_is_uniform_and_does_not_store(client, body):
    sid = create(client)
    response = client.post(f"/api/v1/sessions/{sid}/messages", json=body)
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "INVALID_INPUT", "message": "请检查输入类型、长度与必填项。"}}
    assert client.get(f"/api/v1/sessions/{sid}").json()["messages"] == []


def test_cross_browser_cannot_read_write_or_list_another_session(settings):
    with TestClient(create_app(settings)) as first, TestClient(create_app(settings)) as second:
        sid = create(first)
        create(second)
        assert second.get(f"/api/v1/sessions/{sid}").status_code == 404
        assert second.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户"}).status_code == 404
        assert sid not in [item["id"] for item in second.get("/api/v1/sessions").json()]


def test_missing_cookie_cannot_read(client):
    response = client.get("/api/v1/sessions/not-owned")
    assert response.status_code == 401
    assert "error" in response.json()


def test_cookie_is_not_returned_in_body_and_has_protection(client):
    response = client.post("/api/v1/sessions", json={"customer_type": "personal"})
    assert client.cookies.get(COOKIE) not in response.text
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=strict" in cookie_header


def test_cross_origin_mutation_rejected(client):
    response = client.post("/api/v1/sessions", json={"customer_type": "personal"}, headers={"Origin": "https://evil.test"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_DENIED"


def test_bad_host_has_uniform_error(client):
    response = client.get("/api/v1/health", headers={"Host": "evil.test"})
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.parametrize("method,path", [("get", "/api/v1/missing"), ("put", "/api/v1/health")])
def test_unknown_route_and_method_are_uniform(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code in (404, 405)
    assert set(response.json()) == {"error"}


def test_filter_history_by_customer_type(client):
    personal, enterprise = create(client), create(client, "enterprise")
    assert [item["id"] for item in client.get("/api/v1/sessions?customer_type=enterprise").json()] == [enterprise]
    assert personal != enterprise


def test_retention_is_seven_days_and_cleanup_cascades(client):
    sid = create(client)
    client.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户"})
    with client.app.state.sessions.begin() as db:
        record = db.get(Conversation, sid)
        assert record.expires_at - record.created_at == RETENTION
        record.expires_at = time.time() - 1
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert cleanup_expired(client.app.state.engine) == 1
    with client.app.state.sessions() as db:
        assert db.scalars(select(Message).where(Message.session_id == sid)).first() is None


def test_history_survives_new_app_and_startup_removes_expired(settings):
    with TestClient(create_app(settings)) as first:
        sid = create(first)
        first.post(f"/api/v1/sessions/{sid}/messages", json={"text": "开户"})
        expired = create(first)
        with first.app.state.sessions.begin() as db:
            db.get(Conversation, expired).expires_at = time.time() - 1
        cookies = first.cookies
    with TestClient(create_app(settings)) as second:
        second.cookies.update(cookies)
        history = second.get(f"/api/v1/sessions/{sid}")
        assert history.status_code == 200
        assert len(history.json()["messages"]) == 2
        assert second.get(f"/api/v1/sessions/{expired}").status_code == 404
        with second.app.state.sessions() as db:
            assert db.get(Conversation, expired) is None


def test_health_and_static_headers(client):
    health = client.get("/api/v1/health")
    assert health.json() == {"status": "ok", "mode": "demo", "real_model_verification": "待验"}
    for path in ("/", "/app.js", "/app.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_model_mode_without_configuration_is_honest(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'model.sqlite3'}", mode="model")
    with TestClient(create_app(settings)) as client:
        answer = ask(client, "开户")["answer"]
        assert answer["kind"] == "model_unavailable"
        assert answer["sources"] == []
