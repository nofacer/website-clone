"""CSS reference discovery and rewriting."""

from __future__ import annotations

from sitecloner import css
from sitecloner.mapping import path_key

BASE = "https://example.com/assets/theme.css"


def test_absolute_refs_covers_both_notations() -> None:
    stylesheet = """
    @import "reset.css";
    @import url('print.css');
    .a { background: url(../img/bg.png) no-repeat; }
    .b { background: url( "data:image/gif;base64,R0lGOD" ) }
    .c { background: url(#gradient); }
    """
    assert css.absolute_refs(stylesheet, BASE) == [
        "https://example.com/assets/reset.css",
        "https://example.com/assets/print.css",
        "https://example.com/img/bg.png",
    ]


def test_rewrite_repoints_and_preserves_formatting() -> None:
    stylesheet = (
        "@import 'reset.css';\n.a{background:url( '../img/bg.png' )}\n.b{background:url(#g)}"
    )
    mapping = {
        path_key("https://example.com/assets/reset.css"): "../vendor/reset.css",
        path_key("https://example.com/img/bg.png"): "../../img/bg.png",
    }
    rewritten = css.rewrite(
        stylesheet, BASE, lambda url: mapping.get(path_key(url)) if url else None
    )
    assert rewritten == (
        "@import '../vendor/reset.css';\n"
        ".a{background:url( '../../img/bg.png' )}\n"
        ".b{background:url(#g)}"
    )


def test_rewrite_keeps_unknown_targets_untouched() -> None:
    stylesheet = ".a{background:url(missing.png)}"
    assert css.rewrite(stylesheet, BASE, lambda url: None) == stylesheet


def test_bare_and_data_urls_are_left_alone() -> None:
    stylesheet = ".a{background:url(data:image/gif;base64,R0lGOD)}"
    assert css.absolute_refs(stylesheet, BASE) == []
    assert css.rewrite(stylesheet, BASE, lambda url: "nope") == stylesheet
