"""CLI behaviour: argument handling and output directory preparation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sitecloner.cli import _prepare_output, main


def test_output_directory_is_emptied_but_never_replaced(tmp_path: Path) -> None:
    """Recreating the directory would detach a Docker bind mount from it."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "stale.html").write_text("old", encoding="utf-8")
    (output / "subdir").mkdir()
    (output / "subdir" / "old.png").write_bytes(b"old")
    before = output.stat().st_ino

    _prepare_output(output, clean=True)

    assert output.stat().st_ino == before, "the directory itself must survive"
    assert list(output.iterdir()) == []


def test_no_clean_keeps_existing_files(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.html").write_text("keep", encoding="utf-8")

    _prepare_output(output, clean=False)

    assert (output / "keep.html").read_text() == "keep"


def test_output_is_created_when_missing(tmp_path: Path) -> None:
    output = tmp_path / "fresh"
    _prepare_output(output, clean=True)
    assert output.is_dir()


def test_symlinked_output_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(Exception, match="symlink"):
        _prepare_output(link, clean=True)


def test_invalid_exclude_pattern_exits_with_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["https://example.com/", "-x", "([unclosed"]) == 2


def test_non_http_url_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ftp://example.com/", "-o", str(tmp_path / "out")]) == 2
    assert "not an http" in capsys.readouterr().err
