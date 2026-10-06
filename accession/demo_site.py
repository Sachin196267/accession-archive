"""A small local website for demonstrations and tests.

It exercises every discovery path: robots.txt (with a Disallow rule and a
Sitemap line), a sitemap index pointing to a plain and a gzipped sitemap, an RSS
feed, paginated listings with rel=next, canonical duplicates, redirects, broken
links, tracking parameters, fragments, external links and a page whose links are
only created by JavaScript.

    python -m accession demo-site --port 8765

Visit /_demo/publish to "publish" new posts (and edit/remove a few), which is how
the second-scan demonstration shows new and missing URLs. /_demo/reset undoes it.
"""
import gzip
import hashlib
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"version": 1, "latency": 0.0}
_lock = threading.Lock()

CSS = "body{font:16px/1.5 Georgia,serif;max-width:720px;margin:40px auto;padding:0 16px}nav a{margin-right:12px}"


def posts():
    n = 24 if STATE["version"] == 1 else 30
    out = [i for i in range(1, n + 1)]
    if STATE["version"] > 1:
        out.remove(3)  # an article that was taken down
    return out


def page(title, body, extra_head=""):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title} - Northwind Field Notes</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml" title="RSS">
<link rel="stylesheet" href="/assets/site.css">{extra_head}</head><body>
<nav><a href="/">Home</a><a href="/blog/">Journal</a><a href="/products/">Products</a><a href="/about">About</a>
<a href="/contact#form">Contact</a></nav><h1>{title}</h1>{body}
<footer><a href="https://example.org/" rel="nofollow">Partner site</a> · <a href="mailto:hi@example.test">Email</a></footer>
</body></html>"""


def render(path):
    """Return (status, content_type, body, headers)."""
    v = STATE["version"]
    if path == "/robots.txt":
        return 200, "text/plain", "User-agent: *\nDisallow: /private/\nSitemap: /sitemap_index.xml\n", {}
    if path == "/sitemap_index.xml":
        body = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{o}/sitemap-pages.xml</loc></sitemap><sitemap><loc>{o}/sitemap-posts.xml.gz</loc></sitemap>
</sitemapindex>"""
        return 200, "application/xml", body, {"_origin": True}
    if path == "/sitemap-pages.xml":
        locs = ["/", "/about", "/products/", "/contact", "/legal/privacy", "/legal/terms"]
        body = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>{{o}}{p}</loc></url>" for p in locs) + "</urlset>"
        return 200, "application/xml", body, {"_origin": True}
    if path == "/sitemap-posts.xml.gz":
        body = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>{{o}}/blog/post-{i}</loc><lastmod>2026-0{1 + i % 9}-1{i % 10}</lastmod></url>" for i in posts()
        ) + "".join(f"<url><loc>{{o}}/blog/archive-only-{k}</loc></url>" for k in (1, 2, 3)) + "</urlset>"
        return 200, "application/gzip", body, {"_origin": True, "_gzip": True}
    if path == "/feed.xml":
        items = "".join(f"<item><title>Post {i}</title><link>{{o}}/blog/post-{i}</link></item>" for i in posts()[-8:])
        return 200, "application/rss+xml", f'<?xml version="1.0"?><rss version="2.0"><channel><title>Northwind</title>{items}</channel></rss>', {"_origin": True}
    if path == "/assets/site.css":
        return 200, "text/css", CSS, {}
    if path == "/":
        return 200, "text/html", page("Home", """<p>Notes from the field team.</p><ul>
<li><a href="/blog/">Read the journal</a></li><li><a href="/about?utm_source=home&utm_medium=nav">Who we are</a></li>
<li><a href="/old-about">Our story (old link)</a></li><li><a href="/missing-page">An old promo</a></li>
<li><a href="/server-error">Status page</a></li><li><a href="/private/drafts">Drafts</a></li>
<li><a href="/app/">Interactive map</a></li><li><a href="/products/?sort=price">Products by price</a></li>
<li><a href="/blog/post-1#comments">Comments on the first post</a></li></ul>"""), {}
    if path == "/about":
        text = "We have been walking the coast since 2019." if v == 1 else "We have been walking the coast since 2019. Now with a winter programme."
        return 200, "text/html", page("About", f"<p>{text}</p>"), {}
    if path == "/contact":
        return 200, "text/html", page("Contact", '<p id="form">Write to us.</p>'), {}
    if path in ("/legal/privacy", "/legal/terms"):
        return 200, "text/html", page(path.rsplit("/", 1)[1].title(), "<p>The usual.</p>"), {}
    if path == "/old-about":
        return 301, "text/html", "", {"Location": "/about"}
    if path == "/server-error":
        return 500, "text/html", "<h1>Internal error</h1>", {}
    if path.startswith("/private/"):
        return 200, "text/html", page("Private", "<p>Should never be crawled.</p>"), {}
    if path == "/products/" or path.startswith("/products/?"):
        items = "".join(f'<li><a href="/products/item-{i}">Item {i}</a></li>' for i in range(1, 7))
        return 200, "text/html", page("Products", f"<ul>{items}</ul>", '<link rel="canonical" href="/products/">'), {}
    if path.startswith("/products/item-"):
        return 200, "text/html", page(f"Item {path.rsplit('-', 1)[1]}", '<p>Hand made.</p><a href="/products/">Back</a>'), {}
    if path == "/app/":
        js = """<div id="nav"></div><script>
for (const v of ["dashboard","layers","history"]) { const a=document.createElement("a"); a.href="/app/"+v; a.textContent=v; document.getElementById("nav").append(a, " "); }
</script><noscript>This map needs JavaScript.</noscript>"""
        return 200, "text/html", page("Interactive map", js), {}
    if path.startswith("/app/"):
        return 200, "text/html", page(path.rsplit("/", 1)[1].title(), "<p>Map view.</p>"), {}
    if path.startswith("/blog/") and (path in ("/blog/",) or path.startswith("/blog/page/")):
        ps = list(reversed(posts()))
        n = 1 if path == "/blog/" else int(path.rstrip("/").rsplit("/", 1)[1] or 1)
        pages = (len(ps) + 5) // 6
        chunk = ps[(n - 1) * 6: n * 6]
        if not chunk:
            return 404, "text/html", page("Not found", ""), {}
        lis = "".join(f'<li><a href="/blog/post-{i}">Post {i}</a></li>' for i in chunk)
        nxt = f'<a rel="next" href="/blog/page/{n + 1}/">Older posts</a>' if n < pages else ""
        head = f'<link rel="next" href="/blog/page/{n + 1}/">' if n < pages else ""
        return 200, "text/html", page(f"Journal - page {n}", f"<ul>{lis}</ul>{nxt}", head), {}
    if path.startswith("/blog/post-"):
        try:
            i = int(path.rsplit("-", 1)[1])
        except ValueError:
            i = 0
        if i in posts():
            return 200, "text/html", page(f"Post {i}", f"<p>Field note number {i}.</p><p><a href='/blog/'>All posts</a></p>"), {}
    if path.startswith("/blog/archive-only-"):
        return 200, "text/html", page("From the archive", "<p>Only listed in the sitemap.</p>"), {}
    return 404, "text/html", page("Not found", "<p>Nothing here.</p>"), {}


class Handler(BaseHTTPRequestHandler):
    server_version = "NorthwindDemo/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        if path == "/_demo/publish":
            with _lock:
                STATE["version"] = 2
            return self._send(200, "text/plain", b"published: 6 new posts, /about edited, post-3 removed\n", {})
        if path == "/_demo/reset":
            with _lock:
                STATE["version"] = 1
            return self._send(200, "text/plain", b"reset to version 1\n", {})
        if STATE["latency"]:
            time.sleep(STATE["latency"])  # imitate a real server's response time
        status, ctype, body, headers = render(path)
        origin = f"http://{self.headers.get('Host', 'localhost')}"
        if headers.pop("_origin", False):
            body = body.replace("{o}", origin)
        data = body.encode()
        if headers.pop("_gzip", False):
            data = gzip.compress(data)
        etag = '"' + hashlib.md5(data).hexdigest()[:16] + '"'
        if status == 200 and self.headers.get("If-None-Match") == etag:
            return self._send(304, ctype, b"", {"ETag": etag})
        if status == 200:
            headers["ETag"] = etag
        self._send(status, ctype, data, headers)

    def _send(self, status, ctype, data, headers):
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text") else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Date", formatdate(usegmt=True))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def serve(port=8765, host="127.0.0.1", background=False, latency=0.0):
    STATE["latency"] = latency
    httpd = ThreadingHTTPServer((host, port), Handler)
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    print(f"Demo site on http://{host}:{port}/  (publish new posts: /_demo/publish)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return httpd
