"""Command line entry point for the website cloner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
from pathlib import Path

from .crawler import CloneError, Crawler, Options
from .fetch import DEFAULT_USER_AGENT

log = logging.getLogger("sitecloner")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitecloner",
        description="Recursively mirror a static website into an offline browsable copy.",
    )
    parser.add_argument("url", help="start URL, e.g. https://usegitai.com/")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="directory that receives the mirror (default: output)",
    )
    parser.add_argument(
        "-c", "--concurrency", type=int, default=8, help="parallel requests (default: 8)"
    )
    parser.add_argument("--max-pages", type=int, default=500, help="page limit (default: 500)")
    parser.add_argument(
        "--max-assets", type=int, default=20000, help="asset limit (default: 20000)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per request timeout in seconds"
    )
    parser.add_argument("--retries", type=int, default=2, help="retries per request")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait before each request (politeness)",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        help="skip URLs matching REGEX (repeatable)",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header")
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="ignore HTTP_PROXY/NO_PROXY and system proxy settings",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="keep whatever is already in the output directory",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every asset")
    return parser


def _prepare_output(output: Path, clean: bool) -> None:
    """Create (or empty) the output directory, refusing to touch anything else.

    The directory itself is kept in place and only its contents are removed.  Replacing it
    would break a Docker bind mount, because the container keeps a reference to the old
    (deleted) directory inode and would go on serving a stale, unlinked copy.
    """
    if output.is_symlink():
        raise CloneError(f"refusing to use symlinked output directory: {output}")
    if clean and output.exists():
        if not output.is_dir():
            raise CloneError(f"output path is not a directory: {output}")
        log.info("emptying %s", output)
        for child in output.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run the cloner; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        excludes = tuple(re.compile(pattern) for pattern in args.exclude)
    except re.error as exc:
        print(f"invalid --exclude pattern: {exc}", file=sys.stderr)
        return 2

    output: Path = args.output
    try:
        _prepare_output(output, clean=not args.no_clean)
    except (CloneError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    options = Options(
        concurrency=max(args.concurrency, 1),
        max_pages=max(args.max_pages, 1),
        max_assets=max(args.max_assets, 0),
        retries=max(args.retries, 0),
        timeout=args.timeout,
        delay=args.delay,
        user_agent=args.user_agent,
        trust_env=not args.no_env,
        excludes=excludes,
    )
    crawler = Crawler(args.url, output, options)
    print(f"cloning {args.url} into {output}/ (scope: {crawler.scope.root})")
    try:
        stats = asyncio.run(crawler.run())
    except (CloneError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130

    print(f"done: {stats.summary()}")
    print(f"entry point: {output}/index.html")
    print("serve it with: docker compose up nginx  ->  http://localhost:8080/")
    if stats.limit_hits:
        print(f"note: {stats.limit_hits} links hit --max-pages; raise it to crawl further")
    return 0 if stats.pages else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
