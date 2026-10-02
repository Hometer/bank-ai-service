from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.service import (
    ModelUnavailable,
    answer_question,
    eligible,
    load_knowledge,
    model_select,
    parse_selection,
)


@pytest.mark.parametrize("raw", ['{"knowledge_id":"personal_opening"}', '```json\n{"knowledge_id":"personal_opening"}\n```'])
def test_valid_selection(raw):
    assert parse_selection(raw, {"personal_opening"}) == "personal_opening"


@pytest.mark.parametrize("raw", [
    '{"knowledge_id":"enterprise_opening"}', '{"knowledge_id":"personal_opening","amount":100}',
    '{"knowledge_id":42}', '{}', 'not json',
])
def test_model_output_cannot_expand_contract(raw):
    with pytest.raises(ValueError):
        parse_selection(raw, {"personal_opening"})


def test_null_selection_is_unknown():
    assert parse_selection('{"knowledge_id":null}', {"personal_opening"}) is None


def test_expired_disabled_or_wrong_context_knowledge_is_ineligible():
    items = load_knowledge()
    assert eligible(items, "personal", today="2027-10-02") == []
    assert eligible(items, "personal", today="2026-01-01") == []
    disabled = [item.model_copy(update={"status": "disabled"}) for item in items]
    assert eligible(disabled, "personal") == []
    assert all(item.customer_type == "enterprise" for item in eligible(items, "enterprise"))


def test_no_knowledge_causes_unknown():
    _, answer = answer_question("开户", "personal", [], Settings())
    assert answer.kind == "unknown"
    assert answer.sources == []


def model_settings():
    return Settings(mode="model", model_base_url="https://model.test/v1", model_name="mock-only", model_api_key="test-only-placeholder")


@pytest.mark.parametrize("failure", ["timeout", "bad_json", "unallowed", "rate_limit", "server_error"])
def test_model_failures_retry_at_most_twice(failure):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout")
        if failure == "rate_limit":
            return httpx.Response(429)
        if failure == "server_error":
            return httpx.Response(500)
        content = "bad-json" if failure == "bad_json" else '{"knowledge_id":"not-allowed"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    with pytest.raises(ModelUnavailable):
        model_select("开户", eligible(load_knowledge(), "personal"), model_settings(), factory)
    assert len(calls) == 2


def test_wrong_key_fails_without_repeating_request():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, text="sensitive external error never returned")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    with pytest.raises(ModelUnavailable) as exc:
        model_select("开户", eligible(load_knowledge(), "personal"), model_settings(), factory)
    assert len(calls) == 1
    assert "sensitive" not in str(exc.value)


def test_client_initialization_failure_is_caught():
    attempts = []

    def factory(**kwargs):
        attempts.append(1)
        raise RuntimeError("test-only initialization failure")

    with pytest.raises(ModelUnavailable):
        model_select("开户", eligible(load_knowledge(), "personal"), model_settings(), factory)
    assert len(attempts) == 2


def test_success_after_invalid_output_is_validated():
    attempts = []

    def handle(request):
        attempts.append(1)
        raw = "invalid" if len(attempts) == 1 else '{"knowledge_id":"personal_opening"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}], "usage": {"total_tokens": 12}})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    assert model_select("开户", eligible(load_knowledge(), "personal"), model_settings(), factory) == "personal_opening"
    assert len(attempts) == 2


def test_bad_model_config_never_constructs_network_client():
    def factory(**kwargs):
        raise AssertionError("client must not be constructed")

    with pytest.raises(ModelUnavailable):
        model_select("开户", [], replace(model_settings(), model_api_key=""), factory)


@pytest.mark.parametrize("question", ["验证码是123456", "查余额", "转人工", "银行卡被盗刷了"])
def test_sensitive_or_controlled_routes_never_call_model(question, monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("model must not be called")

    monkeypatch.setattr("app.service.model_select", reject)
    _, answer = answer_question(question, "personal", load_knowledge(), model_settings())
    assert answer.kind != "answered"
