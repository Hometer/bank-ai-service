"""Source-extractive DeepSeek answers with separately approved costs."""

import asyncio
import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError

from app.bank_faq import named_banks, wrong_region
from app.config import Settings
from app.consultation import answer_consultation
from app.consultation_model import construct_client
from app.model_budget import BudgetUnavailable
from app.schemas import Answer, ConsultationContext, CustomerType, Knowledge, Source, StrictModel
from app.v15_budget import Ledger
from app.v15_web import excerpt, fetch_page


class Evidence(StrictModel):
    id: str
    text: str
    source: Source
    origin: Literal["knowledge", "official_web"]
    fetched_at: str | None = None


class Claim(StrictModel):
    source_id: str
    quote: str = Field(min_length=15, max_length=260)


class ModelReply(StrictModel):
    claims: list[Claim] = Field(max_length=3)
    unanswered: bool = Field(strict=True)


class Result(StrictModel):
    answer: Answer
    evidence: list[Evidence]
    processing: Literal["deepseek", "local_fallback", "no_evidence"]
    web_status: Literal["not_needed", "retrieved", "unavailable"]
    intent: Literal["answer", "agent_assist"]
    draft: bool
    context: ConsultationContext


def parse_reply(raw: str, evidence: list[Evidence]) -> ModelReply:
    value = raw.strip()
    if value.startswith("```json\n") and value.endswith("```"):
        value = value[8:-3]
    reply = ModelReply.model_validate_json(value)
    texts = {item.id: item.text for item in evidence}
    if not reply.claims and not reply.unanswered:
        raise ValueError("Empty complete answer")
    for claim in reply.claims:
        if claim.source_id not in texts or claim.quote not in texts[claim.source_id]:
            raise ValueError("Unbound claim")
        original = texts[claim.source_id]
        start = original.index(claim.quote)
        before = original[:start].rstrip(" \t")
        end = start + len(claim.quote)
        if before and before[-1] not in "\n。！？；.!?;":
            raise ValueError("Quote drops sentence prefix")
        if claim.quote[-1] not in "。！？；.!?;" and end != len(original) and original[end] != "\n":
            raise ValueError("Quote cuts a sentence")
    if len({(claim.source_id, claim.quote) for claim in reply.claims}) != len(reply.claims):
        raise ValueError("Duplicate claims")
    return reply


async def generate(
    question: str,
    kind: CustomerType,
    evidence: list[Evidence],
    settings: Settings,
    cancelled: Event | None = None,
) -> ModelReply:
    value = urlsplit(settings.model_base_url)
    if (
        value.scheme != "https"
        or value.hostname != "api.deepseek.com"
        or value.port not in (None, 443)
        or value.path not in ("", "/", "/v1", "/v1/")
        or value.username
        or value.password
        or value.query
        or value.fragment
        or settings.model_name != "deepseek-flash"
        or not settings.model_api_key
    ):
        raise BudgetUnavailable("Approved model configuration missing")
    prompt = Path(__file__).with_name("prompts").joinpath("answer_v15.txt").read_text()
    data = {
        "question": question,
        "customer_type": kind,
        "evidence": [
            {
                "id": e.id,
                "text": e.text[:400],
                "title": e.source.title,
                "role": "peer_guidance" if "同业通用参考" in e.text else "public_evidence",
            }
            for e in evidence
        ],
    }
    payload = {
        "model": settings.model_name,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
    }
    # UTF-8 bytes are a conservative token upper bound, plus framing overhead.
    if len(json.dumps(payload, ensure_ascii=False).encode()) + 512 > 6000:
        raise BudgetUnavailable("Input exceeds approval")
    ledger = Ledger(settings.v15_budget_path)
    async with asyncio.timeout(22):
        for attempt in range(2):
            if cancelled is not None and cancelled.is_set():
                raise ValueError("Task cancelled")
            ticket = ledger.reserve()
            usage: Any = None
            outcome = "failed"
            try:
                client = await construct_client(httpx.AsyncClient, 8)
                async with client:
                    response = await client.post(
                        settings.model_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + settings.model_api_key},
                        json=payload,
                    )
                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        outcome = "request_rejected"
                        raise BudgetUnavailable("Model request rejected")
                    response.raise_for_status()
                    body = response.json()
                    usage = body.get("usage")
                    if body["choices"][0].get("finish_reason") != "stop":
                        raise ValueError("Incomplete output")
                    supplied = [e.model_copy(update={"text": e.text[:400]}) for e in evidence]
                    reply = parse_reply(body["choices"][0]["message"]["content"], supplied)
                    parse_reply(body["choices"][0]["message"]["content"], evidence)
                    outcome = "validated"
                    return reply
            except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError, IndexError):
                if attempt == 1:
                    raise
            finally:
                ledger.finish(ticket, outcome, usage)
    raise ValueError("No valid answer")


def run_answer(
    question: str,
    kind: CustomerType,
    items: list[Knowledge],
    context: ConsultationContext,
    settings: Settings,
    intent: Literal["answer", "agent_assist"],
    cancelled: Event | None = None,
) -> Result:
    _, local, next_context = answer_consultation(
        question, kind, items, replace(settings, mode="demo", model_api_key=""), context
    )
    evidence: list[Evidence] = []
    for source in local.sources:
        for item in items:
            if item.customer_type == kind:
                for fact in item.public_facts:
                    if fact.id in source.article_ids:
                        evidence.append(
                            Evidence(
                                id=fact.id,
                                text=(fact.answer + "\n限制：" + fact.limits)[:800],
                                source=source,
                                origin="knowledge",
                            )
                        )
    evidence = evidence[:3]
    web_status: Literal["not_needed", "retrieved", "unavailable"] = "not_needed"
    needs_web = bool(re.search(r"最新|今天|当前|官网|网上|联网|实时|近期", question)) or local.coverage in (
        "none",
        "partial",
    )
    if wrong_region(question) or len(named_banks(question)) > 1:
        evidence, needs_web = [], False
    if needs_web:
        web_status = "unavailable"
        # Source URLs come only from validated, active knowledge. Explicit bank context wins.
        banks = named_banks(question)
        from app.bank_faq import BANKS, primary_bank
        bank = banks[0] if banks else primary_bank(items)
        terms = [
            x
            for x in (
                "转数快",
                "轉數快",
                "FPS",
                "密码",
                "密碼",
                "password",
                "网银",
                "網上銀行",
                "Internet Banking",
                "结单",
                "結單",
                "statement",
                "开户",
                "開戶",
                "account opening",
                "定期",
                "定存",
                "deposit",
                "贷款",
                "貸款",
                "loan",
                "融资",
                "融資",
                "权限",
                "授权",
                "退款",
                "退回",
                "期限",
                "取消",
            )
            if x in question
        ]
        aliases = {
            "定存": ["定期", "deposit"],
            "退款": ["refund", "refunds"],
            "重置": ["reset", "forgot", "忘記", "重設"],
            "转数快": ["轉數快", "FPS"],
            "密码": ["密碼", "password"],
            "网银": ["網上銀行", "Internet Banking"],
            "结单": ["結單", "statement"],
            "开户": ["開戶", "account opening"],
            "贷款": ["貸款", "loan"],
        }
        terms += [alias for key, values in aliases.items() if key in question for alias in values]
        candidates: list[Source] = list(local.sources)
        if not candidates:
            for item in items:
                if item.customer_type != kind or (item.faq_bank_id or primary_bank(items)) not in (bank, "hkma"):
                    continue
                if any(term.lower() in (item.title + " ".join(item.keywords)).lower() for term in terms):
                    for fact in item.public_facts:
                        candidates.append(
                            Source(
                                id=item.id,
                                title=item.title,
                                version=item.version,
                                status="public_unreviewed",
                                customer_type=kind,
                                references=fact.references,
                                article_ids=[fact.id],
                            )
                        )
        seen: set[str] = set()
        start = time.monotonic()
        fresh: list[Evidence] = []
        for source in candidates:
            for ref in source.references:
                if cancelled is not None and cancelled.is_set():
                    raise ValueError("Task cancelled")
                if ref.url in seen or len(seen) >= 3 or time.monotonic() - start > 8:
                    continue
                # Peer recommendations are not a current BOCOM policy source.
                host = urlsplit(ref.url).hostname or ""
                if not (
                    host in BANKS[bank][2]
                    or host
                    in (
                        "www.hkma.gov.hk",
                        "www.ifec.org.hk",
                        "www.dps.org.hk",
                        "www.news.gov.hk",
                        "www.info.gov.hk",
                    )
                ):
                    continue
                seen.add(ref.url)
                try:
                    text = excerpt(fetch_page(ref.url), terms)
                    if not text:
                        continue
                    stamp = datetime.now(UTC).isoformat()
                    current = source.model_copy(deep=True)
                    current.references = [
                        ref.model_copy(
                            update={
                                "retrieved_on": stamp[:10],
                                "locator": "本轮官网定向获取片段；抓取时间 " + stamp,
                            }
                        )
                    ]
                    current.retrieved_on = stamp[:10]
                    fresh.append(
                        Evidence(
                            id="WEB-" + str(len(fresh) + 1),
                            text=text,
                            source=current,
                            origin="official_web",
                            fetched_at=stamp,
                        )
                    )
                except (httpx.HTTPError, ValueError, OSError):
                    continue
        if fresh:
            evidence = fresh[:3]
            web_status = "retrieved"
        else:
            # Stale local evidence cannot confirm a request for current conditions.
            if re.search(r"最新|今天|当前|实时|近期", question):
                evidence = []
    processing: Literal["deepseek", "local_fallback", "no_evidence"] = "no_evidence"
    answer = local
    if evidence:
        try:
            reply = asyncio.run(generate(question, kind, evidence, settings, cancelled))
            by_id = {e.id: e for e in evidence}
            quotes = [
                ("同业通用参考，非当前咨询银行专属规定：\n" if "同业通用参考" in by_id[claim.source_id].text else "")
                + claim.quote
                for claim in reply.claims
            ]
            selected = {claim.source_id for claim in reply.claims}
            chosen = [e for e in evidence if e.id in selected]
            if any("同业通用参考" in e.text for e in chosen):
                reply.unanswered = True
            sources: list[Source] = []
            for chosen_evidence in chosen:
                if chosen_evidence.source not in sources:
                    sources.append(chosen_evidence.source)
            text = (
                "根据所附公开资料，相关说明如下：\n" + "\n\n".join(quotes)
                if quotes
                else "目前的公开资料不足以回答这个问题。"
            )
            if reply.unanswered:
                text += "\n所问细节仍有资料缺口，具体条件请向所问银行香港官方渠道核对。"
            text += "\n本答复由 DeepSeek 根据资料摘录整理，未获银行审核，未办理业务。"
            answer = Answer(
                kind="answered" if quotes else "unknown",
                mode="model",
                processing="model",
                coverage="partial" if reply.unanswered else "supported",
                text=text,
                sources=sources,
            )
            processing = "deepseek"
        except Exception:
            processing = "local_fallback"
            answer = local.model_copy(
                update={
                    "text": "DeepSeek 未取得有效结果，以下为本地资料参考。\n" + local.text,
                    "processing": "fallback",
                }
            )
            if re.search(r"最新|今天|当前|实时|近期", question):
                answer = Answer(
                    kind="model_unavailable",
                    mode="demo",
                    processing="fallback",
                    coverage="none",
                    text="本轮官网或模型未取得有效结果，不能确认最新条件。请通过所问银行香港官方渠道核对。",
                )
    elif needs_web:
        answer = Answer(
            kind="unknown",
            mode="demo",
            processing="knowledge",
            coverage="none",
            text="官网定向检索没有取得足够的有效依据，暂时无法确认所问事项。请通过所问银行香港官方渠道核对。",
        )
    if intent == "agent_assist":
        answer = answer.model_copy(
            update={
                "text": "【人工辅助草稿 · 未连接人工】\n客户自述问题："
                + question
                + "\n参考答复：\n"
                + answer.text
                + "\n待人工核对：资料是否适用、缺失条件及客户真实情况。此草稿没有发送给坐席。"
            }
        )
    return Result(
        answer=answer,
        evidence=evidence,
        processing=processing,
        web_status=web_status,
        intent=intent,
        draft=intent == "agent_assist",
        context=next_context,
    )
