"""Extract URLs from HTML, sitemaps, feeds and robots.txt.

Only the standard library is used so parsing works the same everywhere and
stays fast on large crawls.
"""
import gzip
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json", "application/json+feed"}


@dataclass
class Link:
    url: str
    source: str  # link | canonical | pagination | feed | alternate | redirect | meta-refresh


@dataclass
class PageInfo:
    title: str | None = None
    canonical: str | None = None
    base: str | None = None
    links: list[Link] = field(default_factory=list)
    noindex: bool = False


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.info = PageInfo()
        self._in_title = False
        self._title = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = {r.lower() for r in a.get("rel", "").split()}
        if tag == "base" and a.get("href") and not self.info.base:
            self.info.base = a["href"]
        elif tag in ("a", "area") and a.get("href"):
            source = "pagination" if rel & {"next", "prev"} else "link"
            self.info.links.append(Link(a["href"], source))
        elif tag == "link" and a.get("href"):
            href = a["href"]
            if "canonical" in rel:
                self.info.canonical = href
                self.info.links.append(Link(href, "canonical"))
            elif rel & {"next", "prev"}:
                self.info.links.append(Link(href, "pagination"))
            elif "alternate" in rel:
                if a.get("type", "").lower() in FEED_TYPES:
                    self.info.links.append(Link(href, "feed"))
                elif a.get("hreflang"):
                    self.info.links.append(Link(href, "alternate"))
        elif tag == "meta":
            name = a.get("name", "").lower()
            if a.get("http-equiv", "").lower() == "refresh":
                m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", a.get("content", ""), re.I)
                if m:
                    self.info.links.append(Link(m.group(1).strip(), "meta-refresh"))
            elif name == "robots" and "noindex" in a.get("content", "").lower():
                self.info.noindex = True
        elif tag == "title" and self.info.title is None:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.info.title = " ".join("".join(self._title).split())[:300] or None

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)


def parse_html(text: str) -> PageInfo:
    p = _Extractor()
    try:
        p.feed(text)
        p.close()
    except Exception:  # malformed markup: keep whatever was collected
        pass
    if p._in_title and p.info.title is None:
        p.info.title = " ".join("".join(p._title).split())[:300] or None
    return p.info


def maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


@dataclass
class SitemapResult:
    pages: list[tuple[str, str | None]] = field(default_factory=list)  # (loc, lastmod)
    sitemaps: list[str] = field(default_factory=list)


def parse_sitemap(body: bytes) -> SitemapResult:
    """Handles <urlset>, <sitemapindex>, gzip and plain-text sitemaps."""
    body = maybe_gunzip(body)
    res = SitemapResult()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", "replace")
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
        if locs:
            is_index = "<sitemapindex" in text
            for loc in locs:
                (res.sitemaps.append(loc) if is_index else res.pages.append((loc, None)))
        else:  # text sitemap: one URL per line
            res.pages = [(ln.strip(), None) for ln in text.splitlines() if ln.strip().startswith("http")]
        return res
    kind = _local(root.tag)
    for entry in root:
        name = _local(entry.tag)
        loc = lastmod = None
        for child in entry:
            cn = _local(child.tag)
            if cn == "loc" and child.text:
                loc = child.text.strip()
            elif cn == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if kind == "sitemapindex" or name == "sitemap":
            res.sitemaps.append(loc)
        else:
            res.pages.append((loc, lastmod))
    return res


def parse_feed(body: bytes) -> list[str]:
    """Item links from RSS 2.0, RSS 1.0 (RDF) and Atom feeds."""
    try:
        root = ET.fromstring(maybe_gunzip(body))
    except ET.ParseError:
        return re.findall(r"<link>\s*(https?://[^<\s]+)\s*</link>", body.decode("utf-8", "replace"))
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "link":
            if el.text and el.text.strip().startswith("http"):
                out.append(el.text.strip())
            href = el.attrib.get("href")
            if href and el.attrib.get("rel", "alternate") == "alternate":
                out.append(href)
        elif name in ("guid", "id") and el.text and el.text.strip().startswith("http"):
            if el.attrib.get("isPermaLink", "true") != "false":
                out.append(el.text.strip())
    return list(dict.fromkeys(out))


def parse_robots(text: str, robots_url: str) -> RobotFileParser:
    rp = RobotFileParser(robots_url)
    rp.parse(text.splitlines())
    return rp


def looks_like_feed(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").lower()
    if "rss" in ct or "atom" in ct:
        return True
    head = body[:500].lstrip().lower()
    return head.startswith(b"<?xml") and (b"<rss" in head or b"<feed" in head or b"<rdf" in head)
