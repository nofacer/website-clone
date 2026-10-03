"""URL canonicalisation, scope checks and URL -> on-disk path mapping.

Every URL that the crawler touches goes through :func:`canonicalize` so that
``https://Example.com:443/a/../b#frag`` and ``https://example.com/b#frag`` end up
sharing one identity.  :func:`url_to_path` turns such a URL into a relative,
URL-encoded path inside the output directory.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import tldextract

#: Schemes that are left untouched inside the mirrored markup.
SKIP_SCHEMES: frozenset[str] = frozenset(
    {"about", "blob", "data", "ftp", "javascript", "mailto", "sms", "tel", "ws", "wss"}
)

#: URL extensions that are stored verbatim as a file name.
FILE_EXTS: frozenset[str] = frozenset(
    [
        "7z",
        "atom",
        "avif",
        "bin",
        "bmp",
        "bz2",
        "css",
        "csv",
        "eot",
        "epub",
        "gif",
        "gz",
        "htm",
        "html",
        "ico",
        "jpe",
        "jpeg",
        "jpg",
        "js",
        "json",
        "jsonld",
        "map",
        "mjs",
        "mp3",
        "mp4",
        "mpeg",
        "oga",
        "ogg",
        "ogv",
        "opus",
        "otf",
        "pdf",
        "png",
        "rar",
        "rtf",
        "svg",
        "svgz",
        "tar",
        "text",
        "ttf",
        "txt",
        "vtt",
        "wav",
        "webm",
        "webmanifest",
        "webp",
        "woff",
        "woff2",
        "xml",
        "xhtml",
        "xsl",
        "zip",
    ]
)

#: ``FILE_EXTS`` minus the ones that represent a navigable document.
ASSET_EXTS: frozenset[str] = FILE_EXTS - {"htm", "html", "xhtml"}

#: Fallback extension used when a URL carries none and the server says nothing useful.
DEFAULT_ASSET_EXT: str = "bin"

#: Extension guess per content type, for URLs that arrive without an extension.
CONTENT_TYPE_EXT: dict[str, str] = {
    "application/font-woff": "woff",
    "application/font-woff2": "woff2",
    "application/javascript": "js",
    "application/json": "json",
    "application/ld+json": "jsonld",
    "application/manifest+json": "webmanifest",
    "application/pdf": "pdf",
    "application/rss+xml": "xml",
    "application/wasm": "wasm",
    "application/x-font-ttf": "ttf",
    "application/xhtml+xml": "xhtml",
    "application/xml": "xml",
    "application/zip": "zip",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "font/otf": "otf",
    "font/ttf": "ttf",
    "font/woff": "woff",
    "font/woff2": "woff2",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/vnd.microsoft.icon": "ico",
    "image/webp": "webp",
    "image/x-icon": "ico",
    "text/css": "css",
    "text/csv": "csv",
    "text/html": "html",
    "text/javascript": "js",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/vtt": "vtt",
    "text/xml": "xml",
    "video/mp4": "mp4",
    "video/ogg": "ogv",
    "video/webm": "webm",
}

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())  # bundled snapshot: never hits the network

_HREF_SAFE = "/:@!$&'()*+,;=-._~"


def _ascii_host(host: str) -> str:
    """Lowercase, trailing-dot-stripped, IDNA-encoded hostname."""
    host = host.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def registrable_domain(host: str) -> str:
    """Return the eTLD+1 of *host* (``example.co.uk``), or *host* itself for bare hosts/IPs."""
    host = _ascii_host(host)
    extracted = _EXTRACT(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return host


@dataclass(frozen=True, slots=True)
class Scope:
    """The set of hosts we are allowed to keep crawling: the start domain and its subdomains."""

    root: str

    @classmethod
    def from_url(cls, url: str) -> Scope:
        host = urlsplit(url).hostname or ""
        return cls(root=registrable_domain(host))

    def contains(self, url: str) -> bool:
        """True when *url* lives on the start domain or one of its subdomains."""
        if not self.root:
            return False
        host = _ascii_host(urlsplit(url).hostname or "")
        if not host:
            return False
        return host == self.root or host.endswith(f".{self.root}")


def _normalise_path(path: str) -> str:
    if not path:
        return "/"
    trailing = path.endswith(("/", "?", "#"))
    normalised = posixpath.normpath(path)
    while "//" in normalised:
        normalised = normalised.replace("//", "/")
    if not normalised.startswith("/"):
        normalised = f"/{normalised}"
    if trailing and not normalised.endswith("/"):
        normalised += "/"
    return normalised


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Resolve *url* against *base* and normalise it.

    Returns ``None`` for anything we must neither fetch nor rewrite (empty values,
    bare fragments, ``mailto:``, ``data:`` ...).
    """
    url = url.strip()
    if not url or url.startswith("#"):
        return None
    parts = urlsplit(urljoin(base, url) if base else url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or scheme in SKIP_SCHEMES:
        return None
    host = parts.hostname
    if not host:
        return None
    host = host.lower().rstrip(".")
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
        netloc = f"{host}:{port}"
    path = _normalise_path(parts.path)
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def without_fragment(url: str) -> str:
    """*url* with its ``#fragment`` removed; fragments are client side only."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def path_key(url: str) -> str:
    """Identity used for de-duplication: scheme, host and path, ignoring query and fragment."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{_normalise_path(parts.path)}"


def url_suffix(url: str) -> str:
    """The ``?query#fragment`` tail that should be re-attached to a rewritten link."""
    parts = urlsplit(url)
    tail = ""
    if parts.query:
        tail += f"?{parts.query}"
    if parts.fragment:
        tail += f"#{parts.fragment}"
    return tail


def extension_of(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name.rpartition(".")[2].lower() if "." in name else ""


def is_html_type(content_type: str | None) -> bool:
    return bool(content_type) and content_type.split(";")[0].strip().lower() in (
        "text/html",
        "application/xhtml+xml",
    )


def is_css_type(content_type: str | None) -> bool:
    return bool(content_type) and content_type.split(";")[0].strip().lower() == "text/css"


def url_to_path(
    url: str,
    *,
    kind: str = "page",
    content_type: str | None = None,
    host_prefix: str = "",
) -> str:
    """Map *url* to a relative, URL-encoded path inside the output directory.

    *kind* is ``"page"`` for navigable documents and ``"asset"`` for everything the
    browser loads without navigation.  ``content_type`` only matters for assets whose
    URL carries no extension.  ``host_prefix`` is used for third-party resources so
    that ``https://cdn.example/app.js`` cannot collide with a same-site ``/cdn.example``.
    """
    path = _normalise_path(urlsplit(url).path).lstrip("/")
    name = path.rsplit("/", 1)[-1]
    ext = name.rpartition(".")[2].lower() if "." in name else ""
    if ext and ext in FILE_EXTS:
        return f"{host_prefix}{path}"
    if kind == "page" or is_html_type(content_type):
        stem = path if not path or path.endswith("/") else f"{path}/"
        return f"{host_prefix}{stem}index.html"
    stem = path if not path or path.endswith("/") else f"{path}/"
    guessed = CONTENT_TYPE_EXT.get((content_type or "").split(";")[0].strip().lower())
    return f"{host_prefix}{stem}index.{guessed or DEFAULT_ASSET_EXT}"


def relative_href(from_path: str, to_path: str, suffix: str = "") -> str:
    """Build the link that points at *to_path* from the document stored at *from_path*."""
    start = posixpath.dirname(from_path) or "."
    relative = posixpath.relpath(to_path, start)
    return f"{quote(unquote(relative), safe=_HREF_SAFE)}{suffix}"


def output_path(root: str, rel_path: str) -> str:
    """Filesystem path (percent escapes decoded, traversal removed) for *rel_path*."""
    safe = [part for part in unquote(rel_path).split("/") if part not in ("", ".", "..")]
    return "/".join((root, *safe))
