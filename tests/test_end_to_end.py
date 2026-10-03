"""End-to-end test: crawl a fixture site over HTTP and verify the mirror.

The fixture exercises the awkward parts of a real site: relative and absolute links,
nested pages, a stylesheet with its own ``url()`` references and a CDN asset, ``<base href>``,
``srcset``, inline styles, a meta refresh, a same-origin ``.php`` style URL and a link
that leaves the site.
"""

from __future__ import annotations

import asyncio
import http.server
import re
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from sitecloner import dom
from sitecloner.crawler import Crawler, Options
from sitecloner.dom import Node

#: Attributes whose values the cloner rewrites, so they are excluded from fingerprints.
_IGNORED = frozenset({"href", "src", "srcset", "poster", "data", "content", "style"})

#: The fixture pages of the most recent :func:`_pages` call, for structural comparisons.
PAGES: dict[str, tuple[str, str]] = {}


def _pages(origin: str, cdn_origin: str) -> dict[str, tuple[str, str]]:
    """The fixture site.

    *origin* and *cdn_origin* are needed because ``<base href>`` and the stylesheet's
    third-party ``url()`` must point at the servers this module actually runs.
    """
    global PAGES
    pages = {
        "/": (
            "text/html; charset=utf-8",
            """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Home</title>
<link rel="stylesheet" href="/assets/site.css">
</head>
<body>
<h1 id="top">Home</h1>
<a href="/about">About</a>
<a href="deep/page.html">Deep</a>
<a href="/feed.xml">Feed</a>
<a href="https://other.example/blog">Offsite</a>
<a href="/links">Links</a>
<a href="#top">Top</a>
<img src="/img/logo.png" srcset="/img/logo.png 1x, /img/logo2x.png 2x" alt="logo">
</body>
</html>""",
        ),
        "/about": (
            "text/html; charset=utf-8",
            """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>About</title></head>
<body><h1>About</h1><a href="/">Home</a>
<p style="background:url(/img/bg.png)"><a href="/download">Get it</a></p>
</body></html>""",
        ),
        "/download": ("text/html; charset=utf-8", "<h1>Download</h1>"),
        # /broken is linked but not served, so it 404s: the mirror must restore the
        # absolute URL instead of leaving a link to a file that was never written.
        "/links": (
            "text/html; charset=utf-8",
            '<a href="/broken">gone</a><a href="/about">here</a>'
            '<img src="/img/missing.png" alt="x">',
        ),
        "/deep/page.html": (
            "text/html; charset=utf-8",
            f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><base href="{origin}/deep/">
<script src="/assets/app.js"></script></head>
<body><meta http-equiv="refresh" content="0; url=/about">
<a href="../assets/site.css">sheet</a>
</body></html>""",
        ),
        "/assets/site.css": (
            "text/css",
            "@import 'theme.css';\n"
            ".a{background:url(../img/bg.png)}\n"
            f'.b{{background:url("{cdn_origin}/pic.png")}}\n'
            ".c{background:url(data:image/gif;base64,AA)}\n",
        ),
        "/assets/theme.css": ("text/css", ".t{color:#333}\n"),
        "/assets/app.js": ("application/javascript", "console.log('hi');\n"),
        "/img/logo.png": ("image/png", "\x89PNG-logo"),
        "/img/logo2x.png": ("image/png", "\x89PNG-logo2x"),
        "/img/bg.png": ("image/png", "\x89PNG-bg"),
        "/feed.xml": ("application/xml", "<feed><entry>/about</entry></feed>"),
    }
    PAGES = pages
    return pages


#: The stand-in CDN, a second server on another port (a different host as far as scoping).
CDN_PAGES: dict[str, tuple[str, str]] = {
    "/pic.png": ("image/png", "\x89PNG-cdn"),
}


def _handler_for(pages: dict[str, tuple[str, str]]) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a request handler serving *pages* (404 for anything else)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            entry = pages.get(self.path)
            if entry is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content_type, body = entry
            # Binary fixtures are given as latin-1 escapes so they survive .encode().
            payload = body.encode("latin-1" if content_type.startswith("image/") else "utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep pytest output clean

    return Handler


def _start(
    pages: dict[str, tuple[str, str]], host: str = "127.0.0.1"
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """Serve *pages* on a free port of *host* and return the server plus its origin."""
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer((host, port), _handler_for(pages))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{port}"


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    """Run the fixture site plus a stand-in CDN, and return the site's base URL."""
    # The CDN lives on a different *hostname* (not just a different port) so that it
    # really is out of the crawler's scope and lands under _external/.
    cdn, cdn_origin = _start(CDN_PAGES, host="localhost")

    # Reserve the site port before serving so the CDN URL can be baked into the stylesheet.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        site_port = probe.getsockname()[1]
    site_origin = f"http://127.0.0.1:{site_port}"

    pages = _pages(site_origin, cdn_origin)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", site_port), _handler_for(pages))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield site_origin
    finally:
        server.shutdown()
        server.server_close()
        cdn.shutdown()
        cdn.server_close()


@pytest.fixture(scope="module")
def mirror(site: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Clone the fixture site once and hand every test the resulting directory."""
    output = tmp_path_factory.mktemp("mirror")
    crawler = Crawler(site, output, Options(concurrency=4, trust_env=False, retries=0))
    stats = asyncio.run(crawler.run())
    assert stats.pages == 5, f"expected 5 pages, crawled {stats.pages} (errors: {stats.errors})"
    # /broken and /img/missing.png are linked but never served, so they 404.
    assert stats.errors == 2, f"expected the two 404s, got {stats.errors} errors"
    return output


def test_entry_point_is_index_html(mirror: Path) -> None:
    assert (mirror / "index.html").is_file()


def test_every_page_is_mirrored(mirror: Path) -> None:
    for expected in (
        "index.html",
        "about/index.html",
        "download/index.html",
        "deep/page.html",
        "links/index.html",
    ):
        assert (mirror / expected).is_file(), f"missing {expected}"


def test_link_to_a_404_is_restored_to_its_absolute_url(mirror: Path, site: str) -> None:
    """A link whose target was never mirrored must not dangle: it goes back to the live URL."""
    document = (mirror / "links/index.html").read_text()
    assert f'href="{site}/broken"' in document, document
    assert f'src="{site}/img/missing.png"' in document, document
    # the document lives in links/, so /about is one level up
    assert 'href="../about/index.html"' in document, "working links stay local"


def test_home_page_links_are_local(mirror: Path) -> None:
    home = (mirror / "index.html").read_text()
    assert 'href="about/index.html"' in home
    assert 'href="deep/page.html"' in home
    assert 'href="feed.xml"' in home
    assert 'href="assets/site.css"' in home
    assert 'src="img/logo.png"' in home
    assert 'srcset="img/logo.png 1x, img/logo2x.png 2x"' in home


def test_offsite_links_and_fragments_survive(mirror: Path) -> None:
    home = (mirror / "index.html").read_text()
    assert 'href="https://other.example/blog"' in home
    assert 'href="#top"' in home


def test_relative_links_are_relative_to_the_document(mirror: Path) -> None:
    about = (mirror / "about/index.html").read_text()
    assert 'href="../index.html"' in about
    assert 'href="../download/index.html"' in about


def test_stylesheet_rewriting(mirror: Path) -> None:
    stylesheet = (mirror / "assets/site.css").read_text()
    assert stylesheet.startswith("@import 'theme.css';\n")
    assert ".a{background:url(../img/bg.png)}" in stylesheet
    assert '.b{background:url("../_external/localhost/pic.png")}' in stylesheet
    assert ".c{background:url(data:image/gif;base64,AA)}" in stylesheet


def test_nested_stylesheet_and_cdn_asset_are_downloaded(mirror: Path) -> None:
    assert (mirror / "assets/theme.css").is_file()
    cdn_copy = mirror / "_external/localhost/pic.png"
    assert cdn_copy.is_file(), "the third-party image was not mirrored"
    assert cdn_copy.read_bytes() == b"\x89PNG-cdn"


def test_base_href_is_neutralised_and_meta_refresh_rewritten(mirror: Path) -> None:
    deep = (mirror / "deep/page.html").read_text()
    assert 'href="./"' in deep
    assert 'content="0; url=../about/index.html"' in deep
    assert 'src="../assets/app.js"' in deep


def test_inline_style_urls_are_rewritten(mirror: Path) -> None:
    assert "url(../img/bg.png)" in (mirror / "about/index.html").read_text()


def test_binary_assets_are_written_verbatim(mirror: Path) -> None:
    assert (mirror / "img/logo.png").read_bytes() == b"\x89PNG-logo"
    assert (mirror / "feed.xml").read_bytes() == b"<feed><entry>/about</entry></feed>"


def test_no_reference_points_at_a_missing_local_file(mirror: Path) -> None:
    """Every relative reference in every mirrored document must resolve to a real file."""
    missing: list[str] = []
    for document in sorted(mirror.rglob("*.html")):
        for raw in _local_references(document.read_text()):
            if "://" in raw or raw.startswith(("#", "mailto:", "javascript:", "/")):
                continue
            target = (document.parent / raw.split("#")[0].split("?")[0]).resolve()
            if not target.exists():
                missing.append(f"{document.relative_to(mirror)} -> {raw}")
    assert not missing, "dangling references:\n" + "\n".join(missing)


def test_document_structure_is_preserved(mirror: Path) -> None:
    """Rewriting must not add, drop or rename a single element.

    The fingerprint records every element with its attribute names but ignores the
    attribute *values*, since those are exactly what the cloner is expected to change.
    """
    for path, source in (
        ("index.html", "/"),
        ("about/index.html", "/about"),
        ("deep/page.html", "/deep/page.html"),
    ):
        expected = _fingerprint(PAGES[source][1])
        actual = _fingerprint((mirror / path).read_text())
        assert actual == expected, f"structure changed for {source}"


def _fingerprint(html: str) -> list[str]:
    """A structural fingerprint of *html*: one entry per element, values ignored."""
    entries: list[str] = []

    def walk(node: Node, depth: int) -> None:
        names = sorted(
            name.lower() for name, _ in dom.attributes(node) if name.lower() not in _IGNORED
        )
        entries.append(f"{'  ' * depth}{node.tag}[{','.join(names)}]")
        for child in node.iter():
            walk(child, depth + 1)

    for node in dom.walk(dom.parse(html)):
        walk(node, 0)
    return entries


def _local_references(document: str) -> list[str]:
    """Pull href/src values out of a document with a deliberately simple scanner."""
    return re.findall(r'(?:href|src)="([^"]*)"', document)
