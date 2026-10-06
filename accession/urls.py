"""URL normalization and scope rules."""
import posixpath
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

# Query parameters that only track the visitor; they never change the page.
TRACKING_PARAM = re.compile(
    r"^(utm_[a-z0-9_]+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|yclid|mc_cid|mc_eid|"
    r"igshid|_ga|_gl|_hsenc|_hsmi|mkt_tok|ref_src|spm|share)$",
    re.I,
)

# Static resources are fetched by the archive services together with the page;
# they are not worth their own capture.
ASSET_EXT = {
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
    ".svg", ".ico", ".bmp", ".tif", ".tiff", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".m4a", ".m4v", ".webm", ".ogg", ".wav", ".mov", ".avi",
    ".zip", ".gz", ".tgz", ".rar", ".7z", ".dmg", ".exe", ".iso", ".apk",
}

PAGINATION = re.compile(r"([?&](page|p|pg|paged|offset|start)=\d+)|(/page/\d+/?$)", re.I)

_UNRESERVED = re.compile(r"%([0-9A-Fa-f]{2})")


def _fix_escapes(s: str) -> str:
    """Uppercase percent escapes and decode the ones that never need escaping."""

    def repl(m):
        ch = chr(int(m.group(1), 16))
        if ch.isascii() and (ch.isalnum() or ch in "-._~"):
            return ch
        return "%" + m.group(1).upper()

    s = _UNRESERVED.sub(repl, s)
    # escape characters that are not legal in a URL at all (spaces, non-ASCII)
    return quote(s, safe="/:@!$&'()*+,;=~%-._?")


def normalize(url: str, base: str | None = None) -> str | None:
    """Return the canonical form used as the identity of a page, or None if unusable."""
    if not url:
        return None
    url = url.strip().replace("\\", "/")
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = p.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path) if path not in ("", "/") else "/"
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    if trailing and not path.endswith("/"):
        path += "/"
    path = _fix_escapes(path)

    params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    query = urlencode(sorted(params), doseq=True) if params else ""

    # Fragments address a position inside the same document, so they are dropped.
    # A hashbang ("#!/page") is the old AJAX routing convention and *is* a page.
    fragment = p.fragment if p.fragment.startswith("!") else ""
    return urlunsplit((scheme, host, path, query, fragment))


def host_of(url: str) -> str:
    """Host plus non-default port of an already normalized URL."""
    try:
        return urlsplit(url).netloc.rpartition("@")[2].lower()
    except ValueError:
        return ""


def site_host(host: str) -> str:
    host = host.lower().strip().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def in_scope(url: str, domain_host: str, include_subdomains: bool) -> bool:
    h = site_host(host_of(url))
    d = site_host(domain_host)
    if h == d:
        return True
    return include_subdomains and h.endswith("." + d)


def is_asset(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return posixpath.splitext(path)[1] in ASSET_EXT


def looks_like_pagination(url: str) -> bool:
    return bool(PAGINATION.search(url))


def parse_domain_input(text: str) -> tuple[str, str] | None:
    """'example.com', 'https://www.example.com/blog' -> (host, root_url)."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        text = "https://" + text
    root = normalize(text)
    if not root:
        return None
    return site_host(host_of(root)), root
