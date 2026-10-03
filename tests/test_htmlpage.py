"""HTML reference collection and rewriting."""

from __future__ import annotations

from sitecloner import dom, htmlpage
from sitecloner.mapping import Scope, path_key

PAGE = "https://example.com/docs/index.html"


def _collect(document: str, url: str = PAGE) -> htmlpage.Refs:
    parser = dom.parse(document)
    return htmlpage.collect(parser, url, Scope.from_url(url).contains)


def _tables(refs: htmlpage.Refs) -> tuple[dict[str, str], dict[str, str]]:
    pages = {path_key(url): f"pages/{index}.html" for index, url in enumerate(refs.pages)}
    assets = {path_key(url): f"assets/{index}.bin" for index, url in enumerate(refs.assets)}
    return pages, assets


def test_classifies_links_assets_and_offsite_urls() -> None:
    refs = _collect(
        """<a href="/about">about</a>
           <a href="/manual.pdf">manual</a>
           <a href="https://other.example/blog">offsite</a>
           <a href="mailto:hi@example.com">mail</a>
           <img src="/img/logo.svg">
           <script src="https://cdn.example.net/lib.js"></script>
           <iframe src="/embed"></iframe>"""
    )
    assert refs.pages == ["https://example.com/about", "https://example.com/embed"]
    assert refs.assets == [
        "https://example.com/manual.pdf",
        "https://example.com/img/logo.svg",
        "https://cdn.example.net/lib.js",
    ]


def test_rewrite_only_touches_in_scope_references() -> None:
    document = """<!DOCTYPE html><html><head>
        <link rel="canonical" href="/about">
        <base href="https://example.com/blog/">
        <style>@import "sub.css";</style>
        </head><body>
        <a href="/docs/page.html#part">local</a>
        <a href="https://other.example/x">offsite</a>
        <img srcset="/a.png 1x, /b.png 2x" src="/a.png">
        <div style="background:url('pic.png')"></div>
        <meta http-equiv="refresh" content="0; url=/next">
        </body></html>"""
    refs = _collect(document, "https://example.com/docs/index.html")
    pages, assets = _tables(refs)
    out = htmlpage.rewrite(refs, "docs/index.html", pages, assets)

    assert 'href="https://other.example/x"' in out
    assert 'src="https://cdn.example.net/lib.js"' not in out
    assert 'href="./"' in out
    assert "1x" in out and "2x" in out
    assert "@import" in out


def test_rewrite_uses_relative_paths_and_keeps_fragments() -> None:
    refs = _collect("""<a href="/about">a</a><a href="#top">t</a><img src="/img/x.png">""")
    pages = {path_key("https://example.com/about"): "about/index.html"}
    assets = {path_key("https://example.com/img/x.png"): "img/x.png"}
    out = htmlpage.rewrite(refs, "index.html", pages, assets)
    assert 'href="about/index.html"' in out
    assert 'href="#top"' in out
    assert 'src="img/x.png"' in out


def test_unknown_targets_keep_their_original_urls() -> None:
    refs = _collect('<img src="/img/x.png">')
    assert htmlpage.rewrite(refs, "index.html", {}, {}) == (
        '<html><head></head><body><img src="/img/x.png"></body></html>'
    )


def test_style_element_survives_rewriting_with_its_attributes() -> None:
    refs = _collect('<style media="print">a{background:url(p.png)}</style>')
    assets = {path_key("https://example.com/docs/p.png"): "p.png"}
    out = htmlpage.rewrite(refs, "docs/index.html", {}, assets)
    assert 'media="print"' in out
    # the mirrored file sits at the root, so from docs/index.html it is one level up
    assert "url(../p.png)" in out


def test_base_href_is_used_for_resolution() -> None:
    refs = _collect('<base href="https://example.com/blog/"><img src="pic.png">')
    assert refs.assets == ["https://example.com/blog/pic.png"]
    assert refs.referenced_assets() == ["https://example.com/blog/pic.png"]


def test_charset_is_forced_to_utf8() -> None:
    parser = dom.parse(
        '<meta charset="gbk"><meta http-equiv="Content-Type" content="text/html; charset=gbk">'
    )
    htmlpage.set_charset(parser, "utf-8")
    assert 'charset="utf-8"' in dom.serialize(parser)
    assert "charset=utf-8" in dom.serialize(parser)


def test_parse_srcset_handles_descriptors() -> None:
    assert htmlpage.parse_srcset(" a.png 1x, b.png 2x ,") == [("a.png", "1x"), ("b.png", "2x")]
