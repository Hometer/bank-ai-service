import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.model_budget import POLICY, BudgetLedger, BudgetUnavailable
from app.schemas import Answer, CustomerType, Knowledge, ModelSelection, Source

logger = logging.getLogger(__name__)
BLOCKED_TEXT = "[疑似敏感信息已屏蔽，请仅使用非敏感演示问题]"
PRIVATE = ("余额", "账单", "流水", "贷款进度", "融资进度", "账户状态", "我的账户", "查询账户")
URGENT = ("盗刷", "丢卡", "银行卡丢", "银行卡遗失", "诈骗", "被冒用", "泄露", "异常扣款")
HUMAN = ("人工", "投诉", "客户经理", "客服人员")
URGENT_NOTICE = (
    "请停止向可疑人员提供信息或转款，并通过你已有的银行官方 App、银行卡背面或正式网站查找紧急服务。"
    "本演示尚未配置目标银行紧急入口，不能挂失、冻结账户或连接安全专席。不要发送密码或验证码。"
)
CONTACT_OR_KEY = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|api[_ -]?key\s*[:=是为]?\s*\S+|"
    r"sk-[A-Za-z0-9_-]{16,}", re.IGNORECASE,
)
CREDENTIAL = r"(?:密码|验证码|口令|password|pin|cvv|cvc)"
DISCLOSURE = re.compile(CREDENTIAL + r"\s*(?:是|为|[:=])\s*([^\s，。；！？,;!?]+)", re.IGNORECASE)
ATTACHED_VALUE = re.compile(CREDENTIAL + r"\s*[A-Za-z0-9_*@#$!.-]{2,}", re.IGNORECASE)
IDENTIFIER = re.compile(r"(?:卡号|银行卡|手机号|身份证|账户号|账号)\s*(?:是|为|[:=])?\s*[A-Za-z0-9][\d\s-]{3,}")
NUMBER = re.compile(r"\d(?:[\s-]*\d){3,}(?:[xX](?!\w))?")
PUBLIC_VALUES = {"什么", "什么意思", "如何获取", "如何发送的", "怎么获取", "怎么重置", "多少位", "几位"}


def private_request(text: str, *, public_knowledge: bool = False) -> bool:
    if not any(word in text for word in PRIVATE):
        return False
    # Public instructions for finding a statement are distinct from accessing a customer's statement.
    if (public_knowledge and re.search(r"对账单|电子结单", text)
            and re.search(r"在哪|哪里|如何|怎么|渠道|下载", text)
            and not re.search(r"我的|给我|帮我|我们的|本公司|余额|流水|查出|明细", text)):
        return False
    return True


def sanitize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    if CONTACT_OR_KEY.search(normalized) or ATTACHED_VALUE.search(normalized) or IDENTIFIER.search(normalized):
        return BLOCKED_TEXT
    for match in DISCLOSURE.finditer(normalized):
        if match.group(1) not in PUBLIC_VALUES:
            return BLOCKED_TEXT
    # Numbers alone remain ambiguous; explicit dates, periods and amounts can be public questions.
    # Long card/contact-like values are blocked even if a unit is appended.
    for match in NUMBER.finditer(normalized):
        value = match.group()
        digits = re.sub(r"[\s-]", "", value)
        after = normalized[match.end():].lstrip()
        if len(digits) <= 10 and re.match(r"(?:年|个月|月|天|万元|元|%|年期)", after):
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                date.fromisoformat(value)
                continue
            except ValueError:
                pass
        return BLOCKED_TEXT
    return text


def load_knowledge(path: Path | None = None) -> list[Knowledge]:
    items = json.loads((path or Path(__file__).with_name("knowledge.json")).read_text())
    result = [Knowledge.model_validate(item) for item in items]
    if len({item.id for item in result}) != len(result):
        raise ValueError("Duplicate knowledge id")
    return result


def eligible(items: list[Knowledge], customer_type: CustomerType, today: str | None = None) -> list[Knowledge]:
    current = today or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return [item for item in items if item.customer_type == customer_type
            and item.status in ("demo_unreviewed", "public_unreviewed") and item.valid_from <= current < item.valid_until]


def parse_selection(raw: str, allowed: set[str]) -> str | None:
    stripped = raw.strip()
    if stripped.startswith("```json\n") and stripped.endswith("```"):
        stripped = stripped[8:-3].strip()
    elif stripped.startswith("```\n") and stripped.endswith("```"):
        stripped = stripped[4:-3].strip()
    selection = ModelSelection.model_validate_json(stripped)
    if selection.knowledge_id is not None and selection.knowledge_id not in allowed:
        raise ValueError("Unknown knowledge selection")
    return selection.knowledge_id


class ModelUnavailable(Exception):
    pass


class ModelInputTooLarge(ModelUnavailable):
    pass


def approved_model_payload(payload: dict, settings: Settings) -> BudgetLedger | None:
    """Shared approval and reservation policy for old and new contracts."""
    address = urlsplit(settings.model_base_url)
    if (not settings.model_api_key or not settings.model_name or address.scheme != "https"
            or not address.hostname or address.username or address.password or address.query or address.fragment):
        raise ModelUnavailable("Model configuration unavailable")
    ledger = None
    if address.hostname == "api.deepseek.com" or settings.model_budget_path is not None:
        if (address.hostname != "api.deepseek.com" or address.port not in (None, 443)
                or address.path not in ("", "/", "/v1", "/v1/")
                or settings.model_name != POLICY[6] or settings.model_budget_path is None):
            raise ModelUnavailable("Approved model configuration unavailable")
        payload["thinking"] = {"type": "disabled"}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 512 > POLICY[4]:
            raise ModelInputTooLarge("Model input exceeds approved size")
        ledger = BudgetLedger(settings.model_budget_path)
    return ledger


def model_select(
    text: str,
    candidates: list[Knowledge],
    settings: Settings,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> str | None:
    """Dormant unless explicitly configured. Never returns model-authored financial content."""
    address = urlsplit(settings.model_base_url)
    if (not settings.model_api_key or not settings.model_name or address.scheme != "https"
            or not address.hostname or address.username or address.password or address.query or address.fragment):
        raise ModelUnavailable("Model configuration unavailable")
    prompt = (Path(__file__).parent / "prompts/select_knowledge.txt").read_text()
    allowed = {item.id for item in candidates}
    payload = {
        "model": settings.model_name,
        "temperature": 0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({
                "question": text,
                "candidates": [{"id": item.id, "title": item.title,
                                "keywords": item.keywords} for item in candidates],
            }, ensure_ascii=False)},
        ],
    }
    ledger = approved_model_payload(payload, settings)
    for attempt in range(min(2, max(1, settings.model_attempts))):
        started = time.monotonic()
        outcome = "failed"
        ticket = None
        usage = None
        status_code = None
        selected = None
        try:
            if ledger is not None:
                ticket = ledger.reserve(attempt + 1)
            with client_factory(timeout=min(15, max(1, settings.model_timeout)), trust_env=False) as client:
                response = client.post(
                    settings.model_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.model_api_key},
                    json=payload,
                )
                status_code = response.status_code
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    outcome = "request_rejected"
                    raise ModelUnavailable("Model request rejected")
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage", {})
                raw = data["choices"][0]["message"]["content"]
                outcome = "invalid_output"
                selected = parse_selection(raw, allowed)
                tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
                logger.info("model_tokens=%s", tokens if isinstance(tokens, int) else "unknown")
                outcome = "validated"
                return selected
        except BudgetUnavailable:
            raise ModelUnavailable("Approved model budget unavailable") from None
        except ModelUnavailable:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
            # Construction, transport and schema errors get bounded retry without logging payloads.
            pass
        except Exception:
            # Third-party client construction can fail with an arbitrary exception.
            raise ModelUnavailable("Model initialization failed") from None
        finally:
            duration = int((time.monotonic() - started) * 1000)
            if ledger is not None and ticket is not None:
                try:
                    ledger.finish(ticket, outcome, usage, status_code, selected, duration)
                except BudgetUnavailable:
                    raise ModelUnavailable("Model budget record unavailable") from None
            logger.info("model_attempt=%d outcome=%s duration_ms=%d", attempt + 1, outcome, duration)

    raise ModelUnavailable("Model validation or transport failed")


def answer_question(
    text: str, customer_type: CustomerType, items: list[Knowledge], settings: Settings,
) -> tuple[str, Answer]:
    safe = sanitize(text)
    mode: Literal["demo", "model"] = "model" if settings.mode == "model" else "demo"
    if any(word in text for word in URGENT):
        return safe, Answer(kind="urgent", mode=mode, text=URGENT_NOTICE)
    if safe == BLOCKED_TEXT:
        return safe, Answer(kind="sensitive_blocked", mode=mode,
                            text="疑似敏感输入已屏蔽，不进入知识问答或模型，也不保存原文。请只输入非敏感演示问题。")
    if any(word in text for word in HUMAN):
        return safe, Answer(kind="human_unavailable", mode=mode,
                            text="本演示尚未接入银行人工队列或投诉系统，未连接人工，也未提交工单。请使用你已有的银行官方服务渠道。")
    if any(word in text for word in PRIVATE):
        return safe, Answer(kind="private_unavailable", mode=mode,
                            text="账户、账单与企业数据查询尚未接入。请前往你已有的银行官方渠道完成认证后查询；这里不会展示模拟余额或业务状态。")
    current = eligible(items, customer_type)
    selected: Knowledge | None = None
    if settings.mode == "model" and current:
        try:
            selected_id = model_select(safe, current, settings)
            selected = next((item for item in current if item.id == selected_id), None)
        except ModelUnavailable:
            return safe, Answer(kind="model_unavailable", mode=mode,
                                text="当前模型配置、请求或结果校验未通过，未获得可确认答案。请使用银行正式渠道；本次未生成业务结论。")
    else:
        ranked = sorted(current, key=lambda item: sum(word in safe for word in item.keywords), reverse=True)
        selected = next((item for item in ranked if any(word in safe for word in item.keywords)), None)
    if selected is None:
        return safe, Answer(kind="unknown", mode=mode,
                            text="当前演示知识没有可确认的答案，无法提供该业务的具体规则、费用或承诺。请查阅目标银行正式资料或咨询其人工服务。")
    return safe, Answer(kind="answered", mode=mode, text=selected.text,
                        sources=[Source(id=selected.id, title=selected.title, version=selected.version,
                                        customer_type=selected.customer_type)])
