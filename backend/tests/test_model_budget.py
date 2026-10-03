import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.model_budget import BudgetLedger, BudgetUnavailable, initialize_budget
from app.service import ModelUnavailable, eligible, load_knowledge, model_select


def test_reservations_survive_new_instances_and_cannot_be_reset(tmp_path):
    path = tmp_path / "budget.sqlite3"
    initialize_budget(path)
    BudgetLedger(path).reserve(1)
    assert BudgetLedger(path).snapshot()["requests"] == 1
    with pytest.raises(FileExistsError):
        initialize_budget(path)
    assert BudgetLedger(path).snapshot()["reserved_yuan"] == 0.0108


def test_concurrent_reservation_limit_is_atomic(tmp_path):
    path = tmp_path / "budget.sqlite3"
    initialize_budget(path)

    def reserve(_):
        try:
            BudgetLedger(path).reserve(1)
            return True
        except BudgetUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(32))) == 20
    snapshot = BudgetLedger(path).snapshot()
    assert snapshot["reserved_yuan"] == 0.216 < snapshot["limit_yuan"]


@pytest.mark.parametrize("bad", ["missing", "corrupt", "changed", "symlink"])
def test_missing_corrupt_or_changed_ledger_fails_closed(tmp_path, bad):
    path = tmp_path / "budget.sqlite3"
    if bad == "corrupt":
        path.write_text("not a database")
    elif bad in ("changed", "symlink"):
        initialize_budget(path)
        if bad == "changed":
            import sqlite3
            with sqlite3.connect(path) as db:
                db.execute("UPDATE policy SET max_requests=200")
        else:
            link = tmp_path / "link.sqlite3"
            link.symlink_to(path)
            path = link
    with pytest.raises(BudgetUnavailable):
        BudgetLedger(path).reserve(1)


def test_unknown_usage_keeps_reservation_and_excess_usage_freezes_run(tmp_path):
    path = tmp_path / "budget.sqlite3"
    initialize_budget(path)
    ledger = BudgetLedger(path)
    ticket = ledger.reserve(1)
    ledger.finish(ticket, "failed", None, None, None, 5)
    snapshot = ledger.snapshot()
    assert snapshot["unknown_usage_requests"] == 1
    assert snapshot["reserved_yuan"] == 0.0108
    ticket = ledger.reserve(2)
    with pytest.raises(BudgetUnavailable):
        ledger.finish(ticket, "validated", {"prompt_tokens": 5001, "completion_tokens": 10}, 200, None, 8)
    with pytest.raises(BudgetUnavailable):
        BudgetLedger(path).reserve(1)


@pytest.mark.parametrize("case", ["timeout", "invalid", "success", "rejected"])
def test_deepseek_attempts_and_usage_recorded_without_payloads(tmp_path, case):
    path = tmp_path / "budget.sqlite3"
    initialize_budget(path)
    settings = Settings(mode="model", model_base_url="https://api.deepseek.com", model_name="deepseek-flash",
                        model_api_key="fake-demo-only-key", model_budget_path=path)
    calls = []

    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 100
        if case == "timeout":
            raise httpx.ReadTimeout("fake-demo-only-key", request=request)
        if case == "rejected":
            return httpx.Response(402, text="fake-demo-only-key")
        content = "not json" if case == "invalid" else '{"knowledge_id":"personal_opening"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    if case == "success":
        assert model_select("开户", eligible(load_knowledge(), "personal"), settings, factory) == "personal_opening"
    else:
        with pytest.raises(ModelUnavailable) as exc:
            model_select("开户", eligible(load_knowledge(), "personal"), settings, factory)
        assert "fake-demo-only-key" not in str(exc.value)
    snapshot = BudgetLedger(path).snapshot()
    assert snapshot["requests"] == len(calls) == (1 if case in ("success", "rejected") else 2)
    assert "fake-demo-only-key" not in json.dumps(snapshot)
    assert "开户" not in json.dumps(snapshot, ensure_ascii=False)
    if case == "success":
        assert snapshot["estimated_yuan_at_peak_rates"] == 0.00028


@pytest.mark.parametrize("bad", ["ledger", "name", "host", "long_input"])
def test_unapproved_model_or_oversized_input_never_sends(tmp_path, bad):
    path = tmp_path / "budget.sqlite3"
    if bad != "ledger":
        initialize_budget(path)
    settings = Settings(mode="model", model_base_url="https://api.deepseek.com", model_name="deepseek-flash",
                        model_api_key="fake-demo-only-key", model_budget_path=path)
    if bad == "name":
        settings = replace(settings, model_name="expensive-other-model")
    if bad == "host":
        settings = replace(settings, model_base_url="https://other.test")

    def factory(**kwargs):
        raise AssertionError("must not construct network client")

    with pytest.raises(ModelUnavailable):
        model_select("开" * 2000 if bad == "long_input" else "开户",
                     eligible(load_knowledge(), "personal"), settings, factory)
