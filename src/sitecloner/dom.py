"""A thin, accurately typed wrapper around `selectolax`.

The bundled type stubs disagree with the runtime in a handful of places, so every cast
and ``None`` guard lives here instead of leaking into the crawler:

* :attr:`HTMLParser.html` and :attr:`Node.html` always return ``str`` (stub says ``str | None``).
* :attr:`Node.attributes` values are ``str | None``: a valueless attribute such as
  ``<img data>`` is reported as ``None``.
* :meth:`Node.replace_with` accepts a ``Node`` (the stub only lists ``str | bytes | None``).
* :meth:`Node.text` returns ``str``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from selectolax.parser import HTMLParser, Node


def parse(content: str) -> HTMLParser:
    """Parse *content* as HTML, preserving the original document shape."""
    return HTMLParser(content)


def serialize(parser: HTMLParser) -> str:
    """The whole document as HTML, doctype included."""
    return parser.html or ""


def node_html(node: Node) -> str:
    """The node and its children as HTML."""
    return node.html or ""


def node_text(node: Node) -> str:
    """The concatenated text of the node and its children."""
    return node.text()


def walk(parser: HTMLParser) -> Iterator[Node]:
    """Every element in *parser*, depth first (the parser itself has no ``traverse``)."""
    root = parser.root
    if root is not None:
        yield from root.traverse()


def attributes(node: Node) -> Iterator[tuple[str, str]]:
    """``(name, value)`` pairs, skipping valueless attributes such as ``<img data>``."""
    for name, value in node.attributes.items():
        if isinstance(value, str):
            yield name, value


def get_attr(node: Node, name: str, default: str = "") -> str:
    """The value of *name*, or *default* when it is absent or valueless."""
    value = node.attributes.get(name)
    return value if isinstance(value, str) else default


def set_attr(node: Node, name: str, value: str) -> None:
    """Set attribute *name* to *value*, adding it when absent."""
    node.attrs[name] = value


def has_attr(node: Node, name: str) -> bool:
    return name in node.attributes


def replace_with_node(node: Node, replacement: Node) -> None:
    """Swap *node* for *replacement* (which stays linked to its own parser)."""
    cast("Any", node).replace_with(replacement)


__all__ = [
    "HTMLParser",
    "Node",
    "attributes",
    "get_attr",
    "has_attr",
    "node_html",
    "node_text",
    "parse",
    "replace_with_node",
    "serialize",
    "set_attr",
    "walk",
]
