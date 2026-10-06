"""Local snapshot store - the repository's own copy of each page.

Each capture is the page body (or the browser-rendered DOM when the domain has
JavaScript rendering on), gzip-compressed on disk and indexed in `snapshots`.
Content hashes make change detection and snapshot comparison possible without
depending on a third-party service.
"""
import gzip
import hashlib
import re

from .. import config, db, render
from ..fetch import fetch
from .base import ArchiveService, ServiceResult

TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class LocalSnapshot(ArchiveService):
    name = "local"
    label = "Local snapshot"
    description = "Stored in this repository (raw or rendered HTML)"

    def configured(self, settings):
        return True, "gzip copies kept next to the database"

    async def submit(self, sub) -> ServiceResult:
        url = sub["url"]
        rendered = False
        if sub["render_js"] and render.available() and self.engine.renderer:
            page = await self.engine.renderer.render(url)
            if page.error:
                return ServiceResult("retry", error=page.error)
            if page.login_wall:
                return ServiceResult("failed", http_status=page.status,
                                     error="page redirects to a login screen; not captured")
            body, status, rendered = page.html.encode("utf-8"), page.status, True
        else:
            res = await fetch(self.engine.client, url)
            if res.error:
                return ServiceResult("retry", error=res.error)
            if res.final_status >= 500 or res.final_status == 429:
                return ServiceResult("retry", http_status=res.final_status, error=f"HTTP {res.final_status}")
            if res.final_status >= 400:
                return ServiceResult("failed", http_status=res.final_status, error=f"HTTP {res.final_status}")
            body, status = res.body, res.final_status

        digest = hashlib.sha256(body).hexdigest()
        con = self.engine.con
        prev = db.one(con, "SELECT id, content_hash FROM snapshots WHERE url_id = ? ORDER BY id DESC LIMIT 1", sub["url_id"])
        packed = gzip.compress(body, compresslevel=6)
        if config.SNAPSHOTS_IN_DB:
            rel, data = "db", packed
        else:
            rel, data = f"{sub['domain_id']}/{sub['url_id']}/{int(db.now())}-{digest[:12]}.html.gz", None
            path = config.SNAPSHOT_DIR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(packed)
        m = TITLE.search(body[:200_000].decode("utf-8", "replace"))
        title = " ".join(m.group(1).split())[:300] if m else None
        cur = con.execute(
            """INSERT INTO snapshots (url_id, submission_id, captured_at, http_status, content_hash, size, path, rendered,
                                    title, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sub["url_id"], sub["id"], db.now(), status, digest, len(body), rel, int(rendered), title, data),
        )
        note = "rendered DOM" if rendered else "raw HTML"
        if prev:
            note += ", unchanged since previous snapshot" if prev["content_hash"] == digest else ", content changed"
        return ServiceResult("success", archive_url=f"/snapshots/{cur.lastrowid}", archive_id=digest[:16],
                             http_status=status, note=note)


def read_snapshot(snap) -> bytes:
    """Body of a snapshot row, whether it is stored in the database or on disk."""
    if snap["data"] is not None:
        return gzip.decompress(snap["data"])
    return gzip.decompress((config.SNAPSHOT_DIR / snap["path"]).read_bytes())
