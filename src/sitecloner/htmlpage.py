"""HTML link extraction and rewriting.

The document is parsed once with `selectolax` (through :mod:`sitecloner.dom`) and only
the attributes that carry a URL are touched, so tags, comments, whitespace and inline
scripts survive the round trip.  Rewriting happens in two passes: :func:`collect` reports
every reference, the crawler downloads the referenced files, and :func:`rewrite` then
points each reference at the mirrored file.
"""

from __future__ import annotations

import html as html_module
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import css as css_tools
from . import dom
from .dom import HTMLParser, Node
from .mapping import (
    ASSET_EXTS,
    canonicalize,
    extension_of,
    path_key,
    relative_href,
    url_suffix,
)

KIND_PAGE: str = "page"
KIND_ASSET: str = "asset"

#: Attributes whose value is a single URL we mirror.
URL_ATTRS: frozenset[str] = frozenset({"href", "src", "poster", "data", "xlink:href"})

#: Tags that are links the reader may follow.
PAGE_TAGS: frozenset[str] = frozenset({"a", "area"})

#: Tags that embed another document in the page.
FRAME_TAGS: frozenset[str] = frozenset({"iframe", "frame"})

_META_REFRESH = re.compile(
    r"^(?P<head>.*?\burl\s*=\s*)(?P<quote>[\"']?)(?P<url>.*?)(?P=quote)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CHARSET_PARAM = re.compile(r"charset\s*=\s*[^\s;]+", re.IGNORECASE)


@dataclass(slots=True)
class AttrRef:
    """A URL attribute that must be repointed at a mirrored file."""

    node: Node
    attr: str
    url: str
    kind: str


@dataclass(slots=True)
class SrcsetRef:
    """A ``srcset`` attribute: a comma separated list of ``url descriptor`` pairs."""

    node: Node
    attr: str
    entries: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class CssRef:
    """CSS to rewrite, either an inline ``style`` attribute or a ``<style>`` element."""

    node: Node
    attr: str | None
    css: str


@dataclass(slots=True)
class MetaRefreshRef:
    """``<meta http-equiv="refresh" content="0; url=...">``."""

    node: Node
    url: str
    quote: str
    kind: str


@dataclass(slots=True)
class Refs:
    """Everything one HTML document references."""

    parser: HTMLParser
    page_url: str
    attrs: list[AttrRef] = field(default_factory=list)
    srcsets: list[SrcsetRef] = field(default_factory=list)
    styles: list[CssRef] = field(default_factory=list)
    meta_refreshes: list[MetaRefreshRef] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)

    def referenced_assets(self) -> list[str]:
        """Every URL that must be downloaded before this document can be written."""
        found: dict[str, None] = dict.fromkeys(self.assets)
        for style in self.styles:
            for url in css_tools.absolute_refs(style.css, self.page_url):
                found[url] = None
        for srcset in self.srcsets:
            for url, _ in srcset.entries:
                if url:
                    found[url] = None
        return list(found)


def base_href(parser: HTMLParser) -> str | None:
    """The document's ``<base href>``, which every other relative reference resolves against."""
    for node in parser.css("base"):
        href = dom.get_attr(node, "href")
        if href:
            return canonicalize(href)
    return None


def neutralise_base(parser: HTMLParser) -> None:
    """Rewrite ``<base href>`` so leftover relative references stay inside the mirror.

    Every URL we know about is rewritten to a concrete local path, but URLs that
    JavaScript builds at runtime still follow the base element; pointing it at the
    document's own directory keeps those from escaping to the live site.
    """
    for node in parser.css("base"):
        if dom.has_attr(node, "href"):
            dom.set_attr(node, "href", "./")


def set_charset(parser: HTMLParser, charset: str) -> None:
    """Force the document's declared charset, since we always store UTF-8 bytes."""
    for node in parser.css("[charset]"):
        dom.set_attr(node, "charset", charset)
    for node in parser.css("meta[http-equiv]"):
        if dom.get_attr(node, "http-equiv").strip().lower() != "content-type":
            continue
        content = dom.get_attr(node, "content")
        if "charset=" in content.lower():
            dom.set_attr(node, "content", _CHARSET_PARAM.sub(f"charset={charset}", content))


def _classify(node: Node, url: str) -> str:
    if node.tag in FRAME_TAGS:
        return KIND_PAGE
    if node.tag in PAGE_TAGS:
        return KIND_ASSET if extension_of(url) in ASSET_EXTS else KIND_PAGE
    return KIND_ASSET


def parse_srcset(value: str) -> list[tuple[str, str]]:
    """Split a ``srcset`` value into ``(url, descriptor)`` pairs."""
    entries: list[tuple[str, str]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        url, _, descriptor = chunk.partition(" ")
        entries.append((url, descriptor.strip()))
    return entries


def _collect_srcset(node: Node, attr: str, value: str, base: str | None, refs: Refs) -> None:
    entries: list[tuple[str, str]] = []
    for url, descriptor in parse_srcset(value):
        absolute = canonicalize(url, base) if url else None
        if absolute:
            entries.append((absolute, descriptor))
            refs.assets.append(absolute)
        else:
            entries.append((url, descriptor))
    if entries:
        refs.srcsets.append(SrcsetRef(node=node, attr=attr, entries=entries))


def collect(parser: HTMLParser, page_url: str, crawlable: Callable[[str], bool]) -> Refs:
    """Find every reference in *parser*, classifying each as a page or an asset.

    Links that leave the site are left untouched (an ``<a href>`` keeps its absolute
    URL), while third-party assets are still reported so that they can be mirrored.
    """
    refs = Refs(parser=parser, page_url=page_url)
    # Relative references resolve against <base href> when present, else against the
    # document's own URL.
    base = base_href(parser) or page_url

    for node in dom.walk(parser):
        for attr, raw in list(dom.attributes(node)):
            if node.tag == "base" and attr == "href":
                continue
            if attr == "style":
                refs.styles.append(CssRef(node=node, attr="style", css=raw))
                continue
            if attr == "srcset":
                _collect_srcset(node, attr, raw, base, refs)
                continue
            if attr not in URL_ATTRS:
                continue
            url = canonicalize(raw, base)
            if url is None:
                continue
            kind = _classify(node, url)
            if kind == KIND_PAGE and not crawlable(url):
                continue  # off-site link: keep the original absolute URL
            refs.attrs.append(AttrRef(node=node, attr=attr, url=url, kind=kind))
            (refs.pages if kind == KIND_PAGE else refs.assets).append(url)

        if node.tag == "style":
            refs.styles.append(CssRef(node=node, attr=None, css=dom.node_text(node)))

    for node in parser.css("meta[http-equiv]"):
        if dom.get_attr(node, "http-equiv").strip().lower() != "refresh":
            continue
        match = _META_REFRESH.match(dom.get_attr(node, "content"))
        if match is None:
            continue
        url = canonicalize(match.group("url"), base)
        if url is None:
            continue
        kind = KIND_ASSET if extension_of(url) in ASSET_EXTS else KIND_PAGE
        if kind == KIND_PAGE and not crawlable(url):
            continue
        refs.meta_refreshes.append(
            MetaRefreshRef(node=node, url=url, quote=match.group("quote"), kind=kind)
        )
        (refs.pages if kind == KIND_PAGE else refs.assets).append(url)

    refs.assets = list(dict.fromkeys(refs.assets))
    refs.pages = list(dict.fromkeys(refs.pages))
    return refs


def _replace_style_element(node: Node, css: str) -> None:
    """Swap a ``<style>`` element for one holding *css* (``Node.text`` is read-only)."""
    attributes = "".join(
        f' {name}="{html_module.escape(value, quote=True)}"' for name, value in dom.attributes(node)
    )
    replacement = dom.parse(f"<style{attributes}>{css}</style>").css_first("style")
    if replacement is not None:
        dom.replace_with_node(node, replacement)


def rewrite(
    refs: Refs,
    document_path: str,
    pages: dict[str, str],
    assets: dict[str, str],
) -> str:
    """Repoint every collected reference at its mirrored file and serialise the document.

    *pages* and *assets* map a :func:`~sitecloner.mapping.path_key` to the relative path of
    the mirrored file.  Anything missing from those maps keeps its original URL.
    """

    def target_for(url: str, table: dict[str, str]) -> str | None:
        found = table.get(path_key(url))
        return relative_href(document_path, found, url_suffix(url)) if found else None

    for ref in refs.attrs:
        table = pages if ref.kind == KIND_PAGE else assets
        href = target_for(ref.url, table)
        if href:
            dom.set_attr(ref.node, ref.attr, href)

    for srcset in refs.srcsets:
        candidates: list[str] = []
        for url, descriptor in srcset.entries:
            href = target_for(url, assets) if url else None
            candidates.append(f"{href or url} {descriptor}".strip())
        dom.set_attr(srcset.node, srcset.attr, ", ".join(candidates))

    def resolve_css(url: str | None) -> str | None:
        return target_for(url, assets) if url else None

    for style in refs.styles:
        replacement = css_tools.rewrite(style.css, refs.page_url, resolve_css)
        if style.attr is None:
            _replace_style_element(style.node, replacement)
        else:
            dom.set_attr(style.node, style.attr, replacement)

    for meta in refs.meta_refreshes:
        table = pages if meta.kind == KIND_PAGE else assets
        href = target_for(meta.url, table)
        if not href:
            continue
        quote = f'{meta.quote}"' if meta.quote else ""
        head = _META_REFRESH.match(dom.get_attr(meta.node, "content"))
        prefix = head.group("head") if head else "0; url="
        dom.set_attr(meta.node, "content", f"{prefix}{quote}{href}{quote}")

    neutralise_base(refs.parser)
    return dom.serialize(refs.parser)
