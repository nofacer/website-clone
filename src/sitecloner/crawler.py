"""The recursive crawler: breadth first over pages, mirroring assets on demand."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import css as css_tools
from . import dom, htmlpage
from .dom import HTMLParser
from .fetch import DEFAULT_USER_AGENT, Fetcher, Result, decode
from .mapping import (
    Scope,
    canonicalize,
    extension_of,
    is_css_type,
    output_path,
    path_key,
    relative_href,
    url_suffix,
    url_to_path,
    without_fragment,
)

log = logging.getLogger("sitecloner")
# httpx logs a line per request at INFO, which drowns out the crawl log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class CloneError(RuntimeError):
    """Raised when the clone cannot even start."""


#: Attributes the dangling-reference repair pass looks at.
_REPAIRABLE_ATTRS: frozenset[str] = frozenset({"href", "src", "poster", "data"})


def normalised(rel_path: str) -> str:
    """A stable, comparable form of a relative path (no ``./``, no query)."""
    return posixpath.normpath(rel_path.split("#")[0].split("?")[0]).lstrip("/")


@dataclass(frozen=True, slots=True)
class Options:
    """Knobs for a single clone run."""

    concurrency: int = 8
    max_pages: int = 500
    max_assets: int = 20_000
    retries: int = 2
    timeout: float = 30.0
    delay: float = 0.0
    user_agent: str = DEFAULT_USER_AGENT
    trust_env: bool = True
    excludes: tuple[re.Pattern[str], ...] = ()

    def excluded(self, url: str) -> bool:
        """True when *url* matches one of the ``--exclude`` patterns."""
        return any(pattern.search(url) for pattern in self.excludes)


@dataclass(slots=True)
class Stats:
    """Counters reported when the clone finishes."""

    pages: int = 0
    assets: int = 0
    external_assets: int = 0
    errors: int = 0
    bytes: int = 0
    seconds: float = 0.0
    limit_hits: int = 0

    def summary(self) -> str:
        megabytes = self.bytes / 1_048_576
        return (
            f"pages={self.pages} assets={self.assets} "
            f"(external={self.external_assets}) errors={self.errors} "
            f"size={megabytes:.1f}MiB time={self.seconds:.1f}s"
        )


class Crawler:
    """Mirrors a site breadth first, keeping every in-scope file inside *output*.

    Pages are visited in waves of :attr:`Options.concurrency` requests.  Each page
    downloads the assets it references before it is rewritten and written to disk, so a
    saved document never points at a file that does not exist yet.
    """

    def __init__(self, start_url: str, output: Path, options: Options | None = None) -> None:
        self._start_url = start_url
        self._output = output
        self._options = options or Options()
        self._scope = Scope.from_url(start_url)
        self._root_host = (urlsplit(start_url).hostname or "").lower()
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._mirrored: dict[str, str] = {}
        self._origins: dict[str, str] = {}
        self._written: set[str] = set()
        self._assets_fetched = 0
        self._stats = Stats()

    @property
    def scope(self) -> Scope:
        """The host scope this crawl is limited to."""
        return self._scope

    @property
    def stats(self) -> Stats:
        return self._stats

    def _crawlable(self, url: str) -> bool:
        """True when *url* is an in-scope page we are allowed to follow."""
        return self._scope.contains(url) and not self._options.excluded(url)

    def _enqueue(self, url: str) -> None:
        key = path_key(url)
        if key in self._queued:
            return
        if len(self._queued) >= self._options.max_pages:
            self._stats.limit_hits += 1
            return
        self._queued.add(key)
        # Register the destination up front so that a `<link rel=canonical>` pointing at a
        # page we have not visited yet resolves to the very file it will be written to.
        # Pages rejected by the limit above are deliberately left unregistered, so their
        # links keep pointing at the live site instead of at a file we never write.
        self._mirrored.setdefault(
            key, url_to_path(url, kind="page", host_prefix=self._host_prefix(url))
        )
        self._queue.append(url)

    async def run(self) -> Stats:
        """Crawl the start URL and everything it links to within scope."""
        start = canonicalize(self._start_url)
        if start is None:
            raise CloneError(f"{self._start_url!r} is not an http(s) URL")
        if not self._scope.contains(start):
            raise CloneError(f"cannot determine a site scope for {start!r}")

        self._output.mkdir(parents=True, exist_ok=True)
        options = self._options
        async with httpx.AsyncClient(
            headers={
                "user-agent": options.user_agent,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en,*;q=0.5",
            },
            timeout=httpx.Timeout(options.timeout),
            limits=httpx.Limits(
                max_connections=max(options.concurrency * 2, 10),
                max_keepalive_connections=options.concurrency,
            ),
            follow_redirects=True,
            max_redirects=10,
            trust_env=options.trust_env,
        ) as client:
            fetcher = Fetcher(client, retries=options.retries, delay=options.delay)
            started = time.monotonic()
            self._enqueue(start)
            while self._queue:
                wave = [
                    self._queue.popleft() for _ in range(min(options.concurrency, len(self._queue)))
                ]
                await asyncio.gather(*(self._visit(fetcher, url) for url in wave))
            self._stats.seconds = time.monotonic() - started

        self._repair_dangling_references()
        if self._stats.pages == 0:
            log.error("nothing could be downloaded from %s", start)
        return self._stats

    def _repair_dangling_references(self) -> None:
        """Point local references at the live site again when their target was never written.

        A page can be linked before it turns out to be a 404 (the live site has broken
        links too).  Such a link would otherwise dangle in the mirror, so it is restored to
        the absolute URL it came from, which is what a visitor would get online.
        """
        if not self._written:
            return
        repaired = 0
        for document in sorted(self._output.rglob("*.html")):
            if document.as_posix() not in self._written:
                continue
            rel_path = document.relative_to(self._output).as_posix()
            parser = dom.parse(document.read_text(encoding="utf-8", errors="replace"))
            if self._repair_document(parser, rel_path):
                document.write_text(dom.serialize(parser), encoding="utf-8")
                repaired += 1
        if repaired:
            log.info("restored links to %d page(s) that were never mirrored", repaired)

    def _repair_document(self, parser: HTMLParser, rel_path: str) -> bool:
        """Restore absolute URLs for local references with no file behind them."""
        changed = False
        base = posixpath.dirname(rel_path)
        for node in dom.walk(parser):
            for attr, raw in list(dom.attributes(node)):
                if attr not in _REPAIRABLE_ATTRS:
                    continue
                if not raw or "://" in raw:
                    continue
                if raw.startswith(("#", "mailto:", "data:", "javascript:", "tel:")):
                    continue
                without_suffix = raw.split("#")[0].split("?")[0]
                if not without_suffix:
                    continue
                # A root-absolute reference is already resolved against the mirror root,
                # exactly like nginx would resolve it.
                if without_suffix.startswith("/"):
                    local = without_suffix.lstrip("/")
                else:
                    local = posixpath.normpath(posixpath.join(base, without_suffix))
                if local in ("", "."):
                    continue
                if (self._output / local).exists():
                    continue
                original = self._origins.get(normalised(local))
                if original is None:
                    continue
                dom.set_attr(node, attr, original + url_suffix(raw))
                log.info("dangling reference %r restored to %s", raw, original)
                changed = True
        return changed

    def _write(self, rel_path: str, data: bytes) -> None:
        destination = Path(output_path(str(self._output), rel_path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        self._written.add(destination.as_posix())

    async def _visit(self, fetcher: Fetcher, url: str) -> None:
        result = await fetcher.get(url)
        self._stats.bytes += len(result.content)
        if not result.ok:
            self._stats.errors += 1
            log.warning("skipping %s (HTTP %s)", url, result.status or "network error")
            return

        self._stats.pages += 1
        document_path = url_to_path(url, kind="page", host_prefix=self._host_prefix(url))
        parser = dom.parse(decode(result.content, result.charset))
        if result.charset.lower().replace("_", "-") not in ("utf-8", "utf8"):
            htmlpage.set_charset(parser, "utf-8")

        refs = htmlpage.collect(parser, without_fragment(result.final_url), self._crawlable)
        for link in refs.pages:
            self._enqueue(link)
        pages = {
            path_key(link): self._mirrored.get(
                path_key(link),
                url_to_path(link, kind="page", host_prefix=self._host_prefix(link)),
            )
            for link in refs.pages
        }
        for link in refs.pages:
            # Remember where a local path came from, so a link to a page that never gets
            # written can be restored to its absolute URL later.
            self._remember_origin(pages[path_key(link)], link)
        assets = await self._mirror(fetcher, refs.referenced_assets())
        document = htmlpage.rewrite(refs, document_path, pages, assets)
        self._write(document_path, document.encode("utf-8"))
        log.info("[page %d] %s -> %s", self._stats.pages, url, document_path)

    def _remember_origin(self, local_path: str, url: str) -> None:
        """Record that the mirrored file *local_path* came from *url*."""
        clean = without_fragment(url)
        for suffix in ("?",):
            clean = clean.split(suffix, 1)[0]
        self._origins.setdefault(normalised(local_path), clean)

    async def _mirror(self, fetcher: Fetcher, urls: list[str]) -> dict[str, str]:
        """Download *urls*, returning ``path_key -> local path`` for whatever is available."""
        todo = [
            url
            for url in urls
            if path_key(url) not in self._mirrored
            and not self._options.excluded(url)
            and self._assets_fetched < self._options.max_assets
        ]
        if todo:
            await asyncio.gather(*(self._mirror_one(fetcher, url) for url in todo))
        return {
            key: self._mirrored[key] for url in urls if (key := path_key(url)) in self._mirrored
        }

    def _host_prefix(self, url: str) -> str:
        """Where a URL lives in the mirror.

        * ``""`` for the start host, so ``/about`` stays at ``/about``;
        * ``_hosts/<host>/`` for another host of the same site (a subdomain), because
          ``trust.example.com/about`` must not overwrite the apex site's ``about/``;
        * ``_external/<host>/`` for third-party assets.
        """
        if not self._scope.contains(url):
            host = (urlsplit(url).hostname or "").lower()
            return f"_external/{host}/"
        host = (urlsplit(url).hostname or "").lower()
        return "" if host == self._root_host else f"_hosts/{host}/"

    async def _mirror_one(self, fetcher: Fetcher, url: str) -> None:
        # Record where this file would land before downloading it, so a failed download
        # can still have its references restored to the live URL.
        self._remember_origin(
            url_to_path(url, kind="asset", host_prefix=self._host_prefix(url)), url
        )
        result = await fetcher.get(url)
        self._stats.bytes += len(result.content)
        if not result.ok:
            self._stats.errors += 1
            log.warning("asset failed: %s (HTTP %s)", url, result.status or "network error")
            return

        self._assets_fetched += 1
        prefix = self._host_prefix(url)
        dest = url_to_path(url, kind="asset", content_type=result.mime, host_prefix=prefix)
        content = result.content

        if is_css_type(result.mime) or extension_of(url) == "css":
            content = await self._rewrite_stylesheet(fetcher, result, dest)

        self._mirrored[path_key(url)] = dest
        self._write(dest, content)
        self._stats.assets += 1
        if prefix:
            self._stats.external_assets += 1
        log.debug("asset %s -> %s (%d bytes)", url, dest, len(content))

    async def _rewrite_stylesheet(self, fetcher: Fetcher, result: Result, dest: str) -> bytes:
        """Fetch what a stylesheet references and return its rewritten bytes."""
        stylesheet = decode(result.content, result.charset)
        base = without_fragment(result.final_url)
        nested = await self._mirror(fetcher, css_tools.absolute_refs(stylesheet, base))
        relative = {key: relative_href(dest, value) for key, value in nested.items()}
        rewritten = css_tools.rewrite(
            stylesheet, base, lambda url: relative.get(path_key(url)) if url else None
        )
        return rewritten.encode("utf-8")
