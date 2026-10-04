"""Bounded retrieval from registered official pages, not unrestricted web search."""

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from app.bank_faq import OFFICIAL_HOSTS

HOSTS = OFFICIAL_HOSTS | {
    "eb.hk.bankcomm.com",
    "www.bankcomm.com.hk",
    "www.ifec.org.hk",
    "www.dps.org.hk",
    "www.news.gov.hk",
    "www.info.gov.hk",
}


def safe_url(url: str, resolve: bool = True) -> None:
    value = urlsplit(url)
    if (
        value.scheme != "https"
        or value.hostname not in HOSTS
        or value.port not in (None, 443)
        or value.username
        or value.password
        or value.fragment
    ):
        raise ValueError("Nonofficial URL")
    if resolve:
        addresses = socket.getaddrinfo(value.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("Nonpublic address")


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.parts.append(re.sub(r"\s+", " ", data.strip()))


def fetch_page(url: str) -> str:
    # URLs are selected from validated corpus references, never arbitrary user/model input.
    original = urlsplit(url)
    for _ in range(3):
        safe_url(url)
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                "GET", url, headers={"User-Agent": "FinancialKnowledgePreview/1.5"}
            ) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    target = urlsplit(url)
                    if target.hostname != original.hostname or (
                        original.path.startswith("/hk/") and not target.path.startswith("/hk/")
                    ):
                        raise ValueError("Redirect changed entity or region")
                    continue
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", "").lower():
                    raise ValueError("Unsupported page type")
                content = bytearray()
                for block in response.iter_bytes():
                    content.extend(block)
                    if len(content) > 1_000_000:
                        raise ValueError("Page too large")
                parser = PlainText()
                parser.feed(bytes(content).decode(response.encoding or "utf-8", errors="replace"))
                text = "\n".join(parser.parts)
                if re.search(
                    r"ignore (?:all |previous )?instructions|忽略.{0,8}(?:指令|规则)|reveal.{0,20}api.?key",
                    text,
                    re.I,
                ):
                    raise ValueError("Untrusted page instructions")
                if not text.strip():
                    raise ValueError("Empty page")
                return text
    raise ValueError("Too many redirects")


def excerpt(text: str, terms: list[str], limit: int = 1100) -> str:
    # Prefer actual on-topic body windows over navigation at the start of a bank site.
    lines = text.splitlines()
    generic = {"转数快", "轉數快", "fps", "网银", "網上銀行", "internet banking"}
    scores = {
        i: sum((1 if term.lower() in generic else 4) for term in terms if term.lower() in line.lower())
        for i, line in enumerate(lines)
    }
    matches = sorted((i for i in scores if scores[i]), key=lambda i: (-scores[i], i))
    selected: list[str] = []
    for index in matches:
        for line in lines[max(0, index - 1) : index + 5]:
            if line not in selected:
                selected.append(line)
        if len("\n".join(selected)) >= limit:
            break
    return "\n".join(selected)[:limit] if selected else ""
