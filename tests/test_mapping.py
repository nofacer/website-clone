"""URL canonicalisation, scoping and local path mapping."""

from __future__ import annotations

import pytest

from sitecloner.mapping import (
    Scope,
    canonicalize,
    output_path,
    path_key,
    relative_href,
    url_suffix,
    url_to_path,
    without_fragment,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM:80/About/../about/", "http://example.com/about/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a//b/./c", "https://example.com/a/b/c"),
        ("https://example.com/a?utm=1#top", "https://example.com/a?utm=1#top"),
        ("https://example.com:8443/", "https://example.com:8443/"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "  ", "#anchor", "mailto:a@b.com", "javascript:void(0)", "tel:+1", "data:,x"]
)
def test_canonicalize_rejects_non_http(raw: str) -> None:
    assert canonicalize(raw) is None


def test_canonicalize_resolves_against_base() -> None:
    assert (
        canonicalize("../img/logo.png", "https://example.com/blog/post/")
        == "https://example.com/blog/img/logo.png"
    )


def test_scope_covers_subdomains_only() -> None:
    scope = Scope.from_url("https://www.example.co.uk/blog")
    assert scope.root == "example.co.uk"
    assert scope.contains("https://example.co.uk/x")
    assert scope.contains("https://docs.example.co.uk/x")
    assert not scope.contains("https://notexample.co.uk/x")
    assert not scope.contains("https://example.com/x")


def test_path_key_ignores_query_and_fragment() -> None:
    assert path_key("https://e.com/a?x=1#f") == path_key("https://e.com/a")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/", "index.html"),
        ("https://example.com", "index.html"),
        ("https://example.com/about/", "about/index.html"),
        ("https://example.com/about", "about/index.html"),
        ("https://example.com/about.html", "about.html"),
        ("https://example.com/a/b.php", "a/b.php/index.html"),
        ("https://example.com/static/app.css?v=2", "static/app.css"),
        ("https://example.com/img/%E5%9B%BE.png", "img/%E5%9B%BE.png"),
    ],
)
def test_url_to_path(url: str, expected: str) -> None:
    assert url_to_path(url) == expected


def test_url_to_path_uses_content_type_for_extensionless_assets() -> None:
    assert (
        url_to_path("https://e.com/render?id=3", kind="asset", content_type="image/png")
        == "render/index.png"
    )
    assert (
        url_to_path(
            "https://e.com/render?id=3", kind="asset", content_type="application/octet-stream"
        )
        == "render/index.bin"
    )


def test_url_to_path_prefixes_third_party_hosts() -> None:
    assert (
        url_to_path(
            "https://cdn.example.net/lib/app.js",
            kind="asset",
            content_type="application/javascript",
            host_prefix="_external/cdn.example.net/",
        )
        == "_external/cdn.example.net/lib/app.js"
    )


def test_relative_href_between_nested_documents() -> None:
    assert relative_href("index.html", "index.html") == "index.html"
    assert relative_href("index.html", "about/index.html") == "about/index.html"
    assert relative_href("a/b/index.html", "index.html") == "../../index.html"
    assert relative_href("about/index.html", "static/app.css", "?v=2") == "../static/app.css?v=2"
    assert relative_href("index.html", "img/%E5%9B%BE.png") == "img/%E5%9B%BE.png"


def test_output_path_decodes_and_blocks_traversal() -> None:
    assert output_path("output", "a/b%20c/index.html") == "output/a/b c/index.html"
    assert output_path("output", "../../etc/passwd") == "output/etc/passwd"


def test_url_suffix_and_fragment_stripping() -> None:
    assert url_suffix("https://e.com/a?q=1#f") == "?q=1#f"
    assert without_fragment("https://e.com/a?q=1#f") == "https://e.com/a?q=1"
