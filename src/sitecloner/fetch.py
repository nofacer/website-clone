"""HTTP fetching with retries, redirect following and charset sniffing."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("sitecloner.fetch")

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/126.0.0.0 Safari/537.36 website-clone/0.1 (+offline mirror)"
)

RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

_META_CHARSET = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Result:
    """A single HTTP response, reduced to what the mirror needs."""

    url: str
    status: int
    content: bytes
    content_type: str | None
    charset: str
    final_url: str
    ok: bool

    @property
    def mime(self) -> str:
        return (self.content_type or "").split(";")[0].strip().lower()


def charset_of(content: bytes, content_type: str | None) -> str:
    """Best guess at the character set of *content*: header first, ``<meta>`` second."""
    for parameter in (content_type or "").split(";")[1:]:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip("\"'")
    match = _META_CHARSET.search(content[:4096])
    if match:
        try:
            return match.group(1).decode("ascii")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            return "utf-8"
    return "utf-8"


def decode(content: bytes, charset: str) -> str:
    """Decode *content*, never raising on malformed bytes."""
    try:
        return content.decode(charset)
    except LookupError, UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


class Fetcher:
    """A thin, retrying wrapper around :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retries: int = 2,
        backoff: float = 0.75,
        delay: float = 0.0,
    ) -> None:
        self._client = client
        self._retries = max(retries, 0)
        self._backoff = backoff
        self._delay = max(delay, 0.0)
        self.request_count = 0

    async def get(self, url: str) -> Result:
        """Fetch *url*, retrying transport errors and transient status codes."""
        for attempt in range(self._retries + 1):
            if self._delay:
                await asyncio.sleep(self._delay)
            try:
                self.request_count += 1
                response = await self._client.get(url)
            except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
                log.warning(
                    "request failed (%s/%s) %s: %s: %s",
                    attempt + 1,
                    self._retries + 1,
                    url,
                    type(exc).__name__,
                    exc,
                )
                if attempt == self._retries:
                    return Result(url, 0, b"", None, "utf-8", url, False)
                await asyncio.sleep(self._backoff * 2**attempt)
                continue

            status = response.status_code
            if status in RETRY_STATUSES and attempt < self._retries:
                log.debug("retrying %s after HTTP %s", url, status)
                await asyncio.sleep(self._backoff * 2**attempt)
                continue

            content = response.content
            content_type = response.headers.get("content-type")
            ok = status == httpx.codes.OK
            if not ok and status != httpx.codes.NOT_MODIFIED:
                log.info("HTTP %s for %s", status, url)
            return Result(
                url=url,
                status=status,
                content=content,
                content_type=content_type,
                charset=charset_of(content, content_type),
                final_url=str(response.url),
                ok=ok,
            )

        return Result(url, 0, b"", None, "utf-8", url, False)  # pragma: no cover - defensive
