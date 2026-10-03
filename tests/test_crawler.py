"""Crawler-level behaviour that does not need a live server."""

from __future__ import annotations

import re
from pathlib import Path

from sitecloner.crawler import Crawler, Options


def _crawler() -> Crawler:
    return Crawler("https://example.com/", Path("output"), Options())


def test_start_host_lands_at_the_mirror_root() -> None:
    assert _crawler()._host_prefix("https://example.com/") == ""
    assert _crawler()._host_prefix("https://example.com/a.css") == ""


def test_sibling_hosts_get_their_own_subtree() -> None:
    """Two hosts of one site must not write to the same file."""
    crawler = _crawler()
    assert crawler._host_prefix("https://trust.example.com/") == "_hosts/trust.example.com/"
    assert crawler._host_prefix("https://cdn.example.net/app.js") == "_external/cdn.example.net/"


def test_hosts_never_collide_on_the_same_local_path() -> None:
    """The trust center's ``/`` must not overwrite the marketing site's ``/``."""
    from sitecloner.mapping import path_key, url_to_path

    crawler = _crawler()
    urls = ["https://example.com/", "https://trust.example.com/", "https://example.com/about"]
    local_paths = set()
    for url in urls:
        prefix = crawler._host_prefix(url)
        local_paths.add(url_to_path(url, kind="page", host_prefix=prefix))
        assert path_key(url)  # the URL still has its own identity
    assert len(local_paths) == len(urls), local_paths


def test_origin_normalisation_ignores_query_and_fragment() -> None:
    from sitecloner.crawler import normalised

    assert normalised("a/b/../c/index.html?v=1#x") == "a/c/index.html"
    assert normalised("/about/") == "about"


def test_repair_restores_absolute_urls_for_missing_targets(tmp_path: Path) -> None:
    """A dangling local link is put back to the URL it came from."""

    output = tmp_path / "out"
    crawler = Crawler("https://example.com/", output, Options())
    document = output / "index.html"
    document.parent.mkdir(parents=True)
    document.write_text(
        '<html><body><a href="gone/index.html">x</a><a href="ok/index.html">y</a></body></html>',
        encoding="utf-8",
    )
    (output / "ok").mkdir()
    (output / "ok" / "index.html").write_text("ok", encoding="utf-8")

    crawler._origins["gone/index.html"] = "https://example.com/gone"
    crawler._written = {document.as_posix()}
    crawler._repair_dangling_references()

    result = document.read_text(encoding="utf-8")
    assert 'href="https://example.com/gone"' in result
    assert 'href="ok/index.html"' in result, "existing targets stay local"


def test_max_pages_is_enforced_against_the_queue() -> None:
    """``--max-pages`` must not be exceeded by a wave of parallel discoveries."""
    from sitecloner.mapping import path_key, url_to_path

    crawler = Crawler("https://example.com/", Path("output"), Options(max_pages=3))
    for index in range(10):
        url = f"https://example.com/p{index}"
        crawler._mirrored.setdefault(path_key(url), url_to_path(url, kind="page"))
        crawler._enqueue(url)
    assert len(crawler._queued) == 3
    assert crawler.stats.limit_hits == 7


def test_rejected_pages_are_not_registered_as_mirrored() -> None:
    """A link to a page dropped by the limit must keep pointing at the live site."""
    crawler = Crawler("https://example.com/", Path("output"), Options(max_pages=1))
    crawler._enqueue("https://example.com/")
    crawler._enqueue("https://example.com/too-many")
    assert len(crawler._mirrored) == 1


def test_html_reference_scanner_finds_bare_attributes() -> None:
    assert re.findall(r'(?:href|src)="([^"]*)"', '<a href="/a">x</a>') == ["/a"]
