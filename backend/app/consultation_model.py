"""Bounded v14 routing contract; no model-written answers or additional budget policy."""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.model_budget import BudgetUnavailable
from app.schemas import ConsultationContext, ConsultationRoute, CustomerType, Knowledge
from app.service import ModelUnavailable, approved_model_payload

logger = logging.getLogger(__name__)
TOTAL_TIMEOUT = 30.0


class RouteQuality(Exception):
    """Valid schema but excess/duplicate selections: no paid formatting retry."""


def parse_route(raw: str, allowed: set[str]) -> ConsultationRoute:
    text = raw.strip()
    for opening in ("```json\n", "```\n"):
        if text.startswith(opening) and text.endswith("```"):
            text = text[len(opening):-3].strip()
            break
    route = ConsultationRoute.model_validate_json(text)
    if any(item.id not in allowed for item in route.items):
        raise ValueError("Unallowed candidate")
    if len(route.items) > 2 or len({item.id for item in route.items}) != len(route.items):
        raise RouteQuality("Selection count requires clarification")
    return route


def route_payload(text: str, customer_type: CustomerType, candidates: list[Knowledge],
                  context: ConsultationContext, settings: Settings) -> dict[str, Any]:
    prompt = Path(__file__).with_name("prompts").joinpath("route_consultation_v14.txt").read_text()
    data = {"question": text, "public_object": customer_type,
            "topics": [{"id": topic.knowledge_id, "version": topic.knowledge_version} for topic in context.topics],
            "pending_topics": [{"id": topic.knowledge_id, "version": topic.knowledge_version}
                               for topic in context.pending_topics],
            "clarification_attempts": context.clarification_attempts,
            "candidates": [{"id": item.id, "title": item.title, "keywords": item.keywords} for item in candidates]}
    return {"model": settings.model_name, "temperature": 0, "max_tokens": 100,
            "response_format": {"type": "json_object"}, "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}]}


async def construct_client(factory: Callable[..., httpx.AsyncClient], timeout: float) -> httpx.AsyncClient:
    """Initialization cannot block the loop or extend the request's overall deadline."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[httpx.AsyncClient] = loop.create_future()

    def initialize() -> None:
        try:
            client = factory(timeout=timeout, trust_env=False)
        except Exception as error:
            def reject(error: Exception = error) -> None:
                if not result.done():
                    result.set_exception(error)
            try:
                loop.call_soon_threadsafe(reject)
            except RuntimeError:
                pass
            return

        def deliver() -> None:
            if not result.done():
                result.set_result(client)
            else:
                loop.create_task(client.aclose())
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            asyncio.run(client.aclose())
    threading.Thread(target=initialize, daemon=True).start()
    return await result


async def _model_route(text: str, customer_type: CustomerType, candidates: list[Knowledge],
                       context: ConsultationContext, settings: Settings,
                       client_factory: Callable[..., httpx.AsyncClient]) -> ConsultationRoute:
    ledger = None
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            payload = route_payload(text, customer_type, candidates, context, settings)
            ledger = approved_model_payload(payload, settings)
            allowed = {item.id for item in candidates}
            for attempt in range(min(2, max(1, settings.model_attempts))):
                started = time.monotonic()
                outcome, ticket, usage, status, selected = "failed", None, None, None, None
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    if ledger is not None:
                        ledger.timeout = max(0, min(5, deadline-time.monotonic()))
                        ticket = ledger.reserve(attempt + 1)
                    prepared = await construct_client(client_factory, min(15, max(1, settings.model_timeout)))
                    async with prepared as client:
                        response = await client.post(settings.model_base_url.rstrip("/") + "/chat/completions",
                            headers={"Authorization": "Bearer " + settings.model_api_key}, json=payload)
                        status = response.status_code
                        if 400 <= status < 500 and status != 429:
                            outcome = "request_rejected"
                            raise ModelUnavailable("Model request rejected")
                        response.raise_for_status()
                        data = response.json()
                        usage = data.get("usage", {})
                        outcome = "invalid_output"
                        route = parse_route(data["choices"][0]["message"]["content"], allowed)
                        selected = route.items[0].id if len(route.items) == 1 else None
                        outcome = "validated"
                        return route
                except RouteQuality:
                    outcome = "quality_clarification"
                    return ConsultationRoute(action="clarify", items=[])
                except BudgetUnavailable:
                    raise ModelUnavailable("Approved budget unavailable") from None
                except ModelUnavailable:
                    raise
                except asyncio.CancelledError:
                    outcome = "deadline"
                    raise
                except (httpx.HTTPError, ValidationError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
                    pass
                except Exception:
                    raise ModelUnavailable("Model initialization failed") from None
                finally:
                    duration = int((time.monotonic() - started) * 1000)
                    if ledger is not None and ticket is not None:
                        ledger.timeout = max(0, min(5, deadline-time.monotonic()))
                        try:
                            ledger.finish(ticket, outcome, usage, status, selected, duration)
                        except BudgetUnavailable:
                            raise ModelUnavailable("Model budget record unavailable") from None
                    logger.info("contract=v14 attempt=%d outcome=%s duration_ms=%d", attempt+1, outcome, duration)
    except (TimeoutError, OSError, ValueError, BudgetUnavailable):
        raise ModelUnavailable("Model deadline or configuration unavailable") from None
    raise ModelUnavailable("Model validation or transport failed")


def model_route(text: str, customer_type: CustomerType, candidates: list[Knowledge],
                context: ConsultationContext, settings: Settings,
                client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient) -> ConsultationRoute:
    return asyncio.run(_model_route(text, customer_type, candidates, context, settings, client_factory))
