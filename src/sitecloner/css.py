"""Discovery and rewriting of ``url()`` references and ``@import`` rules in CSS."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .mapping import canonicalize

#: Given an absolute, canonical URL, return the value to inline in the stylesheet.
CssResolver = Callable[[str], str | None]

_URL_RE = re.compile(
    r"""url\(\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^)'"]*?))\s*\)""",
    re.IGNORECASE,
)
_IMPORT_RE = re.compile(
    r"""@import\s+(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CssRef:
    """One URL reference found inside a stylesheet."""

    url: str
    quote: str
    kind: str  # "url" or "import"


def _read_match(match: re.Match[str]) -> tuple[str, str]:
    """Extract the URL and its quote character from a ``url()`` or ``@import`` match."""
    if match.group("double") is not None:
        return match.group("double"), '"'
    if match.group("single") is not None:
        return match.group("single"), "'"
    return (match.group("bare") or "").strip(), ""


def _resolve(raw: str, base_url: str) -> str | None:
    """Canonical absolute URL for a stylesheet reference, or ``None`` to leave it alone."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    return canonicalize(raw, base_url)


def iter_refs(css: str) -> Iterator[CssRef]:
    """Yield every URL referenced by *css*, in both ``url()`` and ``@import`` notation.

    References are yielded in document order regardless of which notation was used.
    """
    matches: list[tuple[int, re.Match[str], str]] = [
        (match.start(), match, "url") for match in _URL_RE.finditer(css)
    ]
    matches.extend((match.start(), match, "import") for match in _IMPORT_RE.finditer(css))
    for _, match, kind in sorted(matches, key=lambda item: item[0]):
        url, quote = _read_match(match)
        yield CssRef(url=url, quote=quote, kind=kind)


def absolute_refs(css: str, base_url: str) -> list[str]:
    """Absolute, fetchable URLs referenced by *css*, resolved against *base_url*."""
    found: dict[str, None] = {}
    for ref in iter_refs(css):
        absolute = _resolve(ref.url, base_url)
        if absolute:
            found[absolute] = None
    return list(found)


def _quote(value: str, quote: str) -> str:
    if quote == '"':
        return f'"{value}"'
    if quote == "'":
        return f"'{value}'"
    return value


def _rewrite_urls(css: str, base_url: str, resolver: CssResolver) -> str:
    def substitute(match: re.Match[str]) -> str:
        raw, quote = _read_match(match)
        absolute = _resolve(raw, base_url)
        replacement = resolver(absolute) if absolute else None
        if not replacement:
            return match.group(0)
        inner = match.group(0)[match.group(0).index("(") + 1 : -1]
        after_open = len(inner) - len(inner.lstrip())
        before_close = len(inner) - len(inner.rstrip())
        pad_open = " " * after_open
        pad_close = " " * before_close
        return f"url({pad_open}{_quote(replacement, quote)}{pad_close})"

    return _URL_RE.sub(substitute, css)


def _rewrite_imports(css: str, base_url: str, resolver: CssResolver) -> str:
    def substitute(match: re.Match[str]) -> str:
        raw, quote = _read_match(match)
        absolute = _resolve(raw, base_url)
        replacement = resolver(absolute) if absolute else None
        if not replacement:
            return match.group(0)
        # The pattern stops at the closing quote, so the original terminator (if any)
        # follows the match and must not be duplicated.
        return f"@import {_quote(replacement, quote)}"

    return _IMPORT_RE.sub(substitute, css)


def rewrite(css: str, base_url: str, resolver: CssResolver) -> str:
    """Return *css* with every resolvable reference replaced by ``resolver``'s answer.

    Whitespace and quoting style around ``url()`` are preserved so that the mirrored
    stylesheet stays as close to the original as possible.
    """
    return _rewrite_imports(_rewrite_urls(css, base_url, resolver), base_url, resolver)
