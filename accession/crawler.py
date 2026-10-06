"""Discovery engine.

A scan walks the domain breadth-first from several seeds at once:

  robots.txt   -> Sitemap: lines, Crawl-delay, Disallow rules
  sitemaps     -> sitemap indexes (recursively, gzip too) and page <loc>s
  feeds        -> RSS / Atom item links
  pages        -> <a>/<area> links, rel=canonical, rel=next/prev, ?page=N style
                  pagination, feed <link>s, hreflang alternates, meta refresh,
                  and (optionally) links that only appear after JavaScript runs

The frontier lives in the database, so a scan survives restarts and several
worker processes can share one scan. Pages that were already downloaded in an
earlier scan are requested conditionally (ETag / Last-Modified); on 304 the
links stored from the previous visit are reused instead of downloading again.
"""
import asyncio
import hashlib
import json
import time
import zlib

from . import config, db, repo
from .fetch import fetch
from .parse import looks_like_feed, parse_feed, parse_html, parse_robots, parse_sitemap
from .urls import in_scope, is_asset, looks_like_pagination, normalize

SITEMAP_GUESSES = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
FEED_GUESSES = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
PROBE = -1  # depth marker for guessed URLs: a 404 there is expected and not recorded
HTML_TYPES = ("text/html", "application/xhtml+xml", "")
LEASE = 120


class Scan:
    def __init__(self, engine, scan_id: int):
        self.engine = engine
        self.con = engine.con
        self.scan_id = scan_id
        self.scan = db.one(self.con, "SELECT * FROM scans WHERE id = ?", scan_id)
        self.domain = dict(db.one(self.con, "SELECT * FROM domains WHERE id = ?", self.scan["domain_id"]))
        self.d_id = self.domain["id"]
        self.robots = None
        self.delay = 0.0
        self._next_slot = 0.0
        self._slot_lock = asyncio.Lock()
        self.fetched = self.scan["pages_fetched"]
        self.capped = bool(self.scan["capped"])
        self.js_only = 0
        self._last_enqueue = time.monotonic()
        self.stopped = False

    # ------------------------------------------------------------------ lifecycle

    async def run(self):
        d = self.domain
        resumed = db.scalar(self.con, "SELECT COUNT(*) FROM frontier WHERE scan_id = ?", self.scan_id) > 0
        db.log(self.con, f"Scan #{self.scan['number']} {'resumed' if resumed else 'started'}", self.d_id)
        await self._load_robots()
        if not resumed:
            self._seed()
        workers = [asyncio.create_task(self._worker(i)) for i in range(max(1, d["crawl_concurrency"]))]
        try:
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()
        state = db.scalar(self.con, "SELECT state FROM scans WHERE id = ?", self.scan_id)
        if state == "running" and not self.engine.stopping:
            self._finish()

    def _seed(self):
        d, root = self.domain, self.domain["root_url"]
        origin = root.split("/", 3)
        origin = f"{origin[0]}//{origin[2]}"
        with db.tx(self.con):
            repo.record_url(self.con, self.d_id, self.scan_id, root, root, "seed", depth=0)
            repo.add_frontier(self.con, self.scan_id, self.d_id, root, "page", 0)
            sitemaps = self.robots.site_maps() if self.robots else None
            for sm in sitemaps or []:
                n = normalize(sm, base=origin + "/")
                if n:
                    repo.add_frontier(self.con, self.scan_id, self.d_id, n, "sitemap", 0)
            if not sitemaps:
                for g in SITEMAP_GUESSES:
                    repo.add_frontier(self.con, self.scan_id, self.d_id, origin + g, "sitemap", PROBE)
            for g in FEED_GUESSES:
                repo.add_frontier(self.con, self.scan_id, self.d_id, origin + g, "feed", PROBE)

    async def _load_robots(self):
        root = self.domain["root_url"]
        parts = root.split("/", 3)
        robots_url = f"{parts[0]}//{parts[2]}/robots.txt"
        res = await fetch(self.engine.client, robots_url)
        if res.ok and res.body:
            self.robots = parse_robots(res.text(), robots_url)
            delay = self.robots.crawl_delay(config.USER_AGENT) or self.robots.crawl_delay("*")
            self.delay = min(float(delay or 0), 30.0)
            with db.tx(self.con):
                for sm in self.robots.site_maps() or []:
                    n = normalize(sm, base=robots_url)
                    if n:
                        repo.add_frontier(self.con, self.scan_id, self.d_id, n, "sitemap", 0)

    def _finish(self):
        c, sid = self.con, self.scan_id
        seen = db.scalar(c, "SELECT COUNT(*) FROM urls WHERE domain_id = ? AND last_scan_id = ?", self.d_id, sid)
        new = db.scalar(c, "SELECT COUNT(*) FROM urls WHERE domain_id = ? AND first_scan_id = ?", self.d_id, sid)
        missing = 0
        if not self.capped:
            missing = db.scalar(
                c, "SELECT COUNT(*) FROM urls WHERE domain_id = ? AND is_external = 0 AND last_scan_id < ?", self.d_id, sid
            )
        t = db.now()
        note = f"{db.plural(self.js_only, 'link')} only visible after JavaScript rendering" if self.js_only else None
        with db.tx(c):
            c.execute(
                """UPDATE scans SET state = 'done', finished_at = ?, urls_seen = ?, urls_new = ?, urls_missing = ?,
                       capped = ?, note = COALESCE(?, note), worker = NULL, lease_until = NULL WHERE id = ?""",
                (t, seen, new, missing, int(self.capped), note, sid),
            )
            next_at = t + self.domain["rescan_hours"] * 3600 if self.domain["rescan_hours"] else None
            c.execute("UPDATE domains SET last_scan_at = ?, next_scan_at = ? WHERE id = ?", (t, next_at, self.d_id))
            # keep the frontier of this scan for statistics, drop older ones
            c.execute("DELETE FROM frontier WHERE domain_id = ? AND scan_id < ?", (self.d_id, sid))
            scan = db.one(c, "SELECT * FROM scans WHERE id = ?", sid)
            took = t - scan["started_at"]
            db.log(
                c,
                f"Scan #{scan['number']} finished in {took:.0f}s: {seen} URLs seen, {new} new, "
                f"{missing} no longer linked, {scan['urls_changed']} changed"
                + (" (page limit reached)" if self.capped else ""),
                self.d_id,
            )
        if self.domain["auto_archive"]:
            repo.enqueue_new(c, self.d_id, include_unfetched=True)
        if self.domain["rearchive_on_change"] and scan["urls_changed"] and scan["number"] > 1:
            repo.enqueue_rearchive(c, self.d_id, repo.domain_services(self.domain),
                                   changed_since=scan["started_at"], reason="changed")

    # ------------------------------------------------------------------ frontier

    def _claim(self):
        t = db.now()
        kinds = "kind != 'page'" if self.capped else "1"
        with db.tx(self.con):
            row = self.con.execute(
                f"""SELECT * FROM frontier WHERE scan_id = ? AND {kinds} AND
                        (state = 'pending' OR (state = 'leased' AND lease_until < ?))
                    ORDER BY CASE kind WHEN 'page' THEN 1 ELSE 0 END, depth, id LIMIT 1""",
                (self.scan_id, t),
            ).fetchone()
            if row:
                self.con.execute(
                    "UPDATE frontier SET state = 'leased', worker = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?",
                    (self.engine.worker_id, t + LEASE, row["id"]),
                )
                self.con.execute(
                    "UPDATE scans SET worker = ?, lease_until = ? WHERE id = ?",
                    (self.engine.worker_id, t + LEASE, self.scan_id),
                )
            return row

    def _outstanding(self) -> int:
        kinds = "AND kind != 'page'" if self.capped else ""
        return db.scalar(
            self.con, f"SELECT COUNT(*) FROM frontier WHERE scan_id = ? AND state IN ('pending', 'leased') {kinds}",
            self.scan_id,
        )

    def _done(self, row, state="done"):
        self.con.execute(
            "UPDATE frontier SET state = ?, done_at = ?, lease_until = NULL WHERE id = ?", (state, db.now(), row["id"])
        )

    async def _worker(self, i):
        idle = 0
        while not self.engine.stopping:
            if db.scalar(self.con, "SELECT state FROM scans WHERE id = ?", self.scan_id) != "running":
                return
            row = self._claim()
            if not row:
                if self._outstanding() == 0:
                    return
                idle += 1
                await asyncio.sleep(min(0.2 * idle, 2))
                continue
            idle = 0
            try:
                if row["kind"] == "sitemap":
                    await self._sitemap(row)
                elif row["kind"] == "feed":
                    await self._feed(row)
                else:
                    await self._page(row)
            except Exception as e:  # never let one URL stop the scan
                with db.tx(self.con):
                    self._done(row, "failed")
                    self.con.execute("UPDATE scans SET errors = errors + 1 WHERE id = ?", (self.scan_id,))
                    db.log(self.con, f"Discovery error on {row['url']}: {type(e).__name__}: {e}", self.d_id, "error")
            self._maybe_enqueue()

    def _maybe_enqueue(self):
        """Feed the archive queue while a long crawl is still running."""
        if self.domain["auto_archive"] and time.monotonic() - self._last_enqueue > 20:
            self._last_enqueue = time.monotonic()
            repo.enqueue_new(self.con, self.d_id, include_unfetched=False)

    async def _polite(self):
        if not self.delay:
            return
        async with self._slot_lock:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = time.monotonic() + self.delay

    # ------------------------------------------------------------------ link handling

    def _accept(self, url, source, found_on, depth, *, lastmod=None, kind="page"):
        """Record a discovered URL and schedule it for crawling when it is in scope."""
        d = self.domain
        if in_scope(url, d["host"], d["include_subdomains"]):
            if is_asset(url):
                return
            if source == "link" and looks_like_pagination(url):
                source = "pagination"
            repo.record_url(self.con, self.d_id, self.scan_id, url, url, source, found_on, depth=depth, lastmod=lastmod)
            if kind == "feed":
                repo.add_frontier(self.con, self.scan_id, self.d_id, url, "feed", depth)
            elif depth <= d["max_depth"]:
                repo.add_frontier(self.con, self.scan_id, self.d_id, url, "page", depth)
        elif d["external_mode"] in ("record", "archive") and not is_asset(url):
            skip = "external link (recorded only)" if d["external_mode"] == "record" else None
            repo.record_url(self.con, self.d_id, self.scan_id, url, url, "external", found_on,
                            external=True, skip_reason=skip, depth=depth)

    # ------------------------------------------------------------------ handlers

    async def _sitemap(self, row):
        res = await fetch(self.engine.client, row["url"])
        with db.tx(self.con):
            if not res.ok or not res.body:
                self._done(row, "skipped" if row["depth"] == PROBE else "failed")
                if row["depth"] != PROBE:
                    db.log(self.con, f"Sitemap {row['url']} unavailable ({res.error or res.final_status})", self.d_id, "warn")
                return
            sm = parse_sitemap(res.body)
            for child in sm.sitemaps:
                n = normalize(child)
                if n:
                    repo.add_frontier(self.con, self.scan_id, self.d_id, n, "sitemap", 0)
            for loc, lastmod in sm.pages:
                n = normalize(loc)
                if n:
                    self._accept(n, "sitemap", row["url"], 1, lastmod=lastmod)
            self._done(row)
            if sm.pages or sm.sitemaps:
                db.log(self.con, f"Sitemap {row['url']}: {len(sm.pages)} pages, {len(sm.sitemaps)} child sitemaps", self.d_id)

    async def _feed(self, row):
        res = await fetch(self.engine.client, row["url"])
        with db.tx(self.con):
            if not res.ok or not looks_like_feed(res.content_type, res.body):
                self._done(row, "skipped" if row["depth"] == PROBE else "failed")
                return
            # a feed that answers is part of the inventory, whether it was linked or guessed
            repo.record_url(self.con, self.d_id, self.scan_id, row["url"], row["url"], "feed", None, depth=1)
            self.con.execute(
                """UPDATE urls SET http_status = ?, final_status = ?, content_type = ?, content_length = ?,
                       response_ms = ?, fetched_at = ? WHERE domain_id = ? AND url = ?""",
                (res.status, res.final_status, res.content_type, len(res.body), res.elapsed_ms, db.now(),
                 self.d_id, row["url"]),
            )
            for link in parse_feed(res.body):
                n = normalize(link, base=row["url"])
                if n:
                    self._accept(n, "feed", row["url"], max(row["depth"], 0) + 1)
            self._done(row)

    async def _page(self, row):
        c, d, url = self.con, self.domain, row["url"]
        u = db.one(c, "SELECT * FROM urls WHERE domain_id = ? AND url = ?", self.d_id, url)
        if u is None:  # e.g. resumed frontier entry whose URL row was deleted
            with db.tx(c):
                self._done(row, "skipped")
            return

        if d["respect_robots"] and self.robots and not self.robots.can_fetch(config.USER_AGENT, url):
            with db.tx(c):
                c.execute("UPDATE urls SET skip_reason = 'disallowed by robots.txt' WHERE id = ?", (u["id"],))
                self._done(row, "skipped")
            return

        if self.fetched >= d["max_pages"]:
            self._cap()
            with db.tx(c):
                self._done(row, "skipped")
            return

        await self._polite()
        conditional = u["outlinks"] is not None
        res = await fetch(self.engine.client, url,
                          etag=u["etag"] if conditional else None,
                          last_modified=u["last_modified"] if conditional else None)
        self.fetched += 1

        links: list[tuple[str, str]] = []
        page_title = canonical = None
        changed = False
        content_hash = u["content_hash"]

        if res.error:
            with db.tx(c):
                if row["attempts"] < 2:
                    c.execute("UPDATE frontier SET state = 'pending', lease_until = NULL WHERE id = ?", (row["id"],))
                else:
                    self._done(row, "failed")
                    c.execute(
                        "UPDATE urls SET fetch_error = ?, fetched_at = ?, response_ms = ? WHERE id = ?",
                        (res.error, db.now(), res.elapsed_ms, u["id"]),
                    )
                    c.execute("UPDATE scans SET errors = errors + 1, pages_fetched = pages_fetched + 1 WHERE id = ?",
                              (self.scan_id,))
            return

        not_modified = res.final_status == 304
        if not_modified:
            links = [tuple(x) for x in json.loads(zlib.decompress(u["outlinks"]))]
        elif res.final_status < 400 and res.content_type in HTML_TYPES and res.body and not res.truncated:
            html = res.text()
            content_hash = hashlib.sha256(res.body).hexdigest()
            changed = bool(u["content_hash"]) and u["content_hash"] != content_hash
            info = parse_html(html)
            page_title, base = info.title, info.base or res.final_url
            for link in info.links:
                n = normalize(link.url, base=base)
                if n and n != url:
                    links.append((n, link.source))
            if info.canonical:
                canonical = normalize(info.canonical, base=base)
            if d["render_js"] and self.engine.renderer:
                links += await self._rendered_links(url, {x[0] for x in links})
        elif res.final_status < 400 and res.body and not res.truncated:
            content_hash = hashlib.sha256(res.body).hexdigest()
            changed = bool(u["content_hash"]) and u["content_hash"] != content_hash

        final = normalize(res.final_url) if res.final_url else None
        t = db.now()
        with db.tx(c):
            skip = u["skip_reason"]
            if not not_modified and skip and skip.startswith(("redirects", "duplicate")):
                skip = None  # re-evaluated below from the fresh response
            if final and final != url and res.redirects:
                if in_scope(final, d["host"], d["include_subdomains"]):
                    self._accept(final, "redirect", url, row["depth"])
                    skip = skip or "redirects to another URL in the inventory"
            if canonical and canonical != url and canonical != final and in_scope(canonical, d["host"], d["include_subdomains"]):
                skip = skip or "duplicate (rel=canonical points elsewhere)"
            c.execute(
                """UPDATE urls SET http_status = ?, final_status = ?, final_url = ?, redirects = ?,
                       content_type = COALESCE(?, content_type), content_length = COALESCE(?, content_length),
                       content_hash = ?, changed_at = CASE WHEN ? THEN ? ELSE changed_at END,
                       title = COALESCE(?, title), canonical_url = COALESCE(?, canonical_url),
                       etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),
                       outlinks = COALESCE(?, outlinks), response_ms = ?, fetched_at = ?, fetch_error = NULL,
                       skip_reason = ?
                   WHERE id = ?""",
                (
                    u["http_status"] if not_modified else res.status,
                    u["final_status"] if not_modified else res.final_status,
                    final if final and final != url else None,
                    res.redirects,
                    None if not_modified else (res.content_type or None),
                    None if not_modified else len(res.body),
                    content_hash, int(changed), t, page_title, canonical,
                    res.headers.get("etag"), res.headers.get("last-modified"),
                    None if not_modified or not links else zlib.compress(json.dumps(links).encode()),
                    res.elapsed_ms, t, skip, u["id"],
                ),
            )
            for link_url, source in links:
                kind = "feed" if source == "feed" else "page"
                self._accept(link_url, source, url, row["depth"] + 1, kind=kind)
            self._done(row)
            c.execute(
                """UPDATE scans SET pages_fetched = pages_fetched + 1, bytes = bytes + ?,
                       not_modified = not_modified + ?, urls_changed = urls_changed + ? WHERE id = ?""",
                (len(res.body), int(not_modified), int(changed), self.scan_id),
            )

    async def _rendered_links(self, url, known: set) -> list[tuple[str, str]]:
        page = await self.engine.renderer.render(url)
        if page.error or not page.html:
            return []
        out = []
        for link in parse_html(page.html).links:
            n = normalize(link.url, base=page.final_url or url)
            if n and n not in known and n != url:
                known.add(n)
                out.append((n, "js-render"))
        self.js_only += len(out)
        return out

    def _cap(self):
        if not self.capped:
            self.capped = True
            with db.tx(self.con):
                self.con.execute("UPDATE scans SET capped = 1 WHERE id = ?", (self.scan_id,))
                self.con.execute(
                    "UPDATE frontier SET state = 'skipped' WHERE scan_id = ? AND kind = 'page' AND state = 'pending'",
                    (self.scan_id,),
                )
                db.log(self.con, f"Page limit ({self.domain['max_pages']}) reached; remaining pages stay in the "
                                 "inventory without being fetched", self.d_id, "warn")

