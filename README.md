# website-clone

Recursively mirror a static website into a directory you can browse **fully offline**, then serve it with nginx in Docker.

```bash
uv run sitecloner https://usegitai.com/        # writes ./output/
docker compose up nginx                        # http://localhost:8080/
```

- Recurses through every link that stays on the start site (same registrable domain + subdomains).
- Downloads **third-party assets too** (CDN js/css/fonts/images) and rewrites the references, so pages render without internet.
- Leaves links to other domains pointing at the real site instead of crawling them.
- Rewrites HTML and CSS in place, preserving the original document structure, so `output/index.html` is the entry point and the page tree is identical to the live one.

## Requirements

- Python 3.14 (managed by [uv](https://docs.astral.sh/uv/))
- Docker (only to serve the result)

## Install

```bash
uv sync
```

## Usage

```bash
uv run sitecloner <START_URL> [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output DIR` | `output` | Directory that receives the mirror (emptied first). |
| `-c, --concurrency N` | `8` | Parallel requests. |
| `--max-pages N` | `500` | Safety limit on pages. |
| `--max-assets N` | `20000` | Safety limit on assets. |
| `--timeout SECONDS` | `30` | Per-request timeout. |
| `--retries N` | `2` | Retries per request. |
| `--delay SECONDS` | `0` | Wait before each request (politeness). |
| `-x, --exclude REGEX` | – | Skip matching URLs (repeatable). |
| `--user-agent UA` | Chrome-like | User-Agent header. |
| `--no-env` | off | Ignore `HTTP_PROXY`/`NO_PROXY` and system proxy settings. |
| `--no-clean` | off | Keep existing files in the output directory. |
| `-q / -v` | – | Quieter / noisier logs. |

> `--no-env` matters when a system proxy is configured: `httpx` honours it by default, and
> such proxies often intercept even `localhost` and `127.0.0.1` requests. |

Examples:

```bash
# smaller mirror, skipping a section that needs auth
uv run siteclorer https://usegitai.com/ -o output --max-pages 200 -x '/blog/tag/'

# mirror into a specific directory and keep the server polite
uv run sitecloner https://usegitai.com/ -o mirrors/usegitai --delay 0.2
```

`output/` always starts with `index.html` (the start URL), e.g.:

```
output/
├── index.html                 # start URL
├── _external/                 # third-party assets, kept apart from same-site paths
│   └── cdn.example.net/lib/app.js
├── about/index.html
├── static/css/site.css        # rewritten: url(../img/x.png) -> ../../img/x.png
└── img/x.png
```

## How the mirror is laid out

```
output/
├── index.html                       # the start URL, always the entry point
├── _hosts/<host>/                   # sibling hosts of the same site (e.g. trust.example.com)
├── _external/<host>/                # third-party assets, kept apart from same-site paths
├── about/index.html                 # extensionless and trailing-slash URLs become directories
├── static/css/site.css              # url() and @import rewritten to the mirrored files
└── img/logo.png
```

Every reference is rewritten to a path **relative to the document that holds it**, so the
mirror also works when opened straight from disk, not only behind nginx.

### What gets rewritten

| In the document | Handling |
| --- | --- |
| `<a href>`, `<iframe src>` | Followed and mirrored when they stay on the site; left absolute when they leave it. |
| `<link>`, `<script>`, `<img>`, `<source>`, `<video>`, `<audio>`, `<object>`, `<use>` | Downloaded (including from third-party CDNs) and repointed. |
| `srcset` | Every candidate is downloaded and rewritten, descriptors preserved. |
| Inline `style="..."` and `<style>` blocks | `url()` and `@import` rewritten in place. |
| `.css` files | Fetched, their references mirrored recursively, then rewritten. |
| `<base href>` | Used to resolve references, then pointed at the document's own directory. |
| `<meta http-equiv="refresh">` | The target URL is rewritten. |
| `mailto:`, `tel:`, `data:`, `javascript:`, `#anchor` | Left exactly as they are. |

Links that were never mirrored — because the live page 404s, or because `--max-pages`
was reached — are restored to their absolute URL instead of pointing at a file that does
not exist, so the mirror never contains a dead internal link.

## Serve the mirror with nginx (Docker)

```bash
docker compose up nginx# http://localhost:8080/
```

`compose.yaml` mounts `./output` at `/usr/share/nginx/html` (read-only) and uses
`docker/nginx.conf`, which declares all the MIME types a static site needs (woff2,
webmanifest, mjs, avif …) and sends `charset utf-8`. Change the published port with
`NGINX_PORT=9000 docker compose up nginx`.

The cloner empties the output directory instead of recreating it, so re-running a clone
keeps the bind mount alive and nginx picks up the new files without a restart.

Equivalently, without compose:

```bash
docker run --rm -p 8080:80 \
  -v "$PWD/output:/usr/share/nginx/html:ro" \
  -v "$PWD/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:1.27-alpine
```

## Development

```bash
make check      # ruff format + ruff check + ty + pytest
make test
make lint
make typecheck
make clone      # clone the default example site into ./output
make serve      # docker compose up nginx
```

Stack: `uv` for the environment, [ruff](https://docs.astral.sh/ruff/) for linting and formatting, [ty](https://github.com/astral-sh/ty) for static type checks, `pytest` for the unit tests.

### Layout

| Path | Purpose |
| --- | --- |
| `src/sitecloner/mapping.py` | URL canonicalisation, site scope, URL → local path. |
| `src/sitecloner/fetch.py` | Retrying `httpx` client, charset sniffing. |
| `src/sitecloner/dom.py` | Thin typed wrapper around `selectolax` (its stubs are inaccurate). |
| `src/sitecloner/htmlpage.py` | HTML link collection + structure-preserving rewrite. |
| `src/sitecloner/css.py` | `url()` / `@import` discovery + rewrite. |
| `src/sitecloner/crawler.py` | Breadth-first orchestration. |
| `src/sitecloner/cli.py` | Command line entry point. |

## Limitations

These are properties of static mirroring, not bugs to be fixed in the cloner:

- **Pages are linked, not discovered.** Only URLs reachable from the start page are
  mirrored; there is no `sitemap.xml` or `robots.txt` discovery.
- **Server-side rendering only.** The mirror contains exactly the HTML the server
  returned. Content that an app fetches later through a live API — a `POST /graphql`
  call, say — cannot be recorded, and such an app may replace the mirrored markup with an
  error screen once it fails. usegitai.com behaves this way: its pages mirror faithfully,
  but its client app needs `POST /graphql` at runtime.
- **URLs built by JavaScript at runtime** are not rewritten. The `<base>` element is
  pointed at the document's own directory so they stay inside the mirror, but assets they
  reference are not downloaded.
- **Client-side state** (theme choice in `localStorage`, cookies, `prefers-color-scheme`)
  is not captured.
- **Forms, logins and other dynamic endpoints** do not work offline.
- **Third-party `<iframe>` embeds** keep pointing at the network.