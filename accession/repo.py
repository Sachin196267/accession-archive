"""Repository operations shared by the web interface, the CLI and the workers."""
import random
import time

from . import db
from .db import now, one, plural, scalar, tx
from .urls import parse_domain_input

SERVICES = ("wayback", "archive_today", "local")
ACTIVE_STATES = ("queued", "running", "retry")
HEARTBEAT_STALE = 20  # seconds without a heartbeat before a worker counts as dead


# --------------------------------------------------------------------------- domains

def add_domain(con, text: str, **opts) -> tuple[int | None, bool]:
    parsed = parse_domain_input(text)
    if not parsed:
        return None, False
    host, root = parsed
    existing = one(con, "SELECT id FROM domains WHERE host = ?", host)
    if existing:
        return existing["id"], False
    cols = {
        "host": host,
        "root_url": root,
        "created_at": now(),
        **{k: v for k, v in opts.items() if v is not None},
    }
    if cols.get("rescan_hours"):
        cols["next_scan_at"] = now() + float(cols["rescan_hours"]) * 3600
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = con.execute(f"INSERT INTO domains ({keys}) VALUES ({marks})", tuple(cols.values()))
    db.log(con, f"Added {host}", cur.lastrowid)
    return cur.lastrowid, True


def domain_services(domain) -> list[str]:
    return [s for s in (domain["services"] or "").split(",") if s in SERVICES]


def running_scan(con, domain_id):
    return one(con, "SELECT * FROM scans WHERE domain_id = ? AND state = 'running' ORDER BY id DESC LIMIT 1", domain_id)


def start_scan(con, domain_id: int, trigger: str = "manual") -> int | None:
    with tx(con):
        if running_scan(con, domain_id):
            return None
        number = (scalar(con, "SELECT MAX(number) FROM scans WHERE domain_id = ?", domain_id) or 0) + 1
        cur = con.execute(
            "INSERT INTO scans (domain_id, number, trigger, started_at) VALUES (?, ?, ?, ?)",
            (domain_id, number, trigger, now()),
        )
        db.log(con, f"Scan #{number} queued ({trigger})", domain_id)
        return cur.lastrowid


def cancel_scan(con, domain_id: int) -> bool:
    with tx(con):
        cur = con.execute(
            "UPDATE scans SET state = 'cancelled', finished_at = ? WHERE domain_id = ? AND state = 'running'",
            (now(), domain_id),
        )
        if cur.rowcount:
            db.log(con, "Scan cancelled by user", domain_id, "warn")
        return cur.rowcount > 0


def delete_domain(con, domain_id: int):
    # explicit deletes: hosted libSQL does not enforce ON DELETE CASCADE by default
    with tx(con):
        con.execute("DELETE FROM snapshots WHERE url_id IN (SELECT id FROM urls WHERE domain_id = ?)", (domain_id,))
        con.execute("DELETE FROM url_sources WHERE url_id IN (SELECT id FROM urls WHERE domain_id = ?)", (domain_id,))
        for table in ("submissions", "frontier", "urls", "scans", "events"):
            con.execute(f"DELETE FROM {table} WHERE domain_id = ?", (domain_id,))
        con.execute("DELETE FROM domains WHERE id = ?", (domain_id,))


# --------------------------------------------------------------------------- urls

def record_url(con, domain_id, scan_id, url, original, source, found_on=None, *,
               external=False, skip_reason=None, depth=None, lastmod=None) -> int:
    t = now()
    row = con.execute(
        """
        INSERT INTO urls (domain_id, url, original_url, source, found_on, first_seen_at, first_scan_id,
                          last_seen_at, last_scan_id, is_external, skip_reason, depth, sitemap_lastmod)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (domain_id, url) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            last_scan_id = excluded.last_scan_id,
            depth = MIN(COALESCE(urls.depth, excluded.depth), COALESCE(excluded.depth, urls.depth)),
            sitemap_lastmod = COALESCE(excluded.sitemap_lastmod, urls.sitemap_lastmod)
        RETURNING id
        """,
        (domain_id, url, original, source, found_on, t, scan_id, t, scan_id, int(external), skip_reason, depth, lastmod),
    ).fetchone()
    con.execute(
        "INSERT OR IGNORE INTO url_sources (url_id, source, found_on, scan_id, seen_at) VALUES (?, ?, ?, ?, ?)",
        (row["id"], source, found_on, scan_id, t),
    )
    return row["id"]


def add_frontier(con, scan_id, domain_id, url, kind="page", depth=0) -> bool:
    cur = con.execute(
        "INSERT OR IGNORE INTO frontier (scan_id, domain_id, url, kind, depth) VALUES (?, ?, ?, ?, ?)",
        (scan_id, domain_id, url, kind, depth),
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- queue

_SUBMITTABLE = """
    u.domain_id = :domain AND u.skip_reason IS NULL
    AND (u.final_status IS NULL OR u.final_status < 400)
    AND u.fetch_error IS NULL
"""


def enqueue_new(con, domain_id: int, services=None, include_unfetched=True) -> int:
    """Queue every URL that this tool has never submitted to a service.

    URLs with any earlier submission (successful, failed or still waiting) are left
    alone; re-archiving is always an explicit request.
    """
    domain = one(con, "SELECT * FROM domains WHERE id = ?", domain_id)
    services = services or domain_services(domain)
    fetched = "" if include_unfetched else "AND u.fetched_at IS NOT NULL"
    total = 0
    with tx(con):
        for svc in services:
            cur = con.execute(
                f"""
                INSERT INTO submissions (url_id, domain_id, service, state, reason, priority, created_at)
                SELECT u.id, u.domain_id, :svc, 'queued', 'new', u.priority, :t FROM urls u
                WHERE {_SUBMITTABLE} {fetched}
                  AND NOT EXISTS (SELECT 1 FROM submissions s
                                  WHERE s.url_id = u.id AND s.service = :svc AND s.state != 'cancelled')
                ORDER BY u.priority DESC, COALESCE(u.depth, 99), u.id
                """,
                {"svc": svc, "t": now(), "domain": domain_id},
            )
            total += cur.rowcount
        if total:
            db.log(con, f"Queued {plural(total, 'new submission')} for {', '.join(label(s) for s in services)}", domain_id)
    return total


def enqueue_rearchive(con, domain_id: int, services, url_ids=None, changed_since=None,
                      reason="rearchive", priority=0) -> int:
    """Ask for a fresh snapshot. Adds a new history entry even if the URL was archived before."""
    where = [_SUBMITTABLE]
    params = {"domain": domain_id, "t": now(), "reason": reason, "prio": priority}
    if url_ids:
        where.append(f"u.id IN ({','.join(str(int(i)) for i in url_ids)})")
    if changed_since is not None:
        where.append("u.changed_at >= :since")
        params["since"] = changed_since
    total = 0
    with tx(con):
        for svc in services:
            params["svc"] = svc
            cur = con.execute(
                f"""
                INSERT INTO submissions (url_id, domain_id, service, state, reason, priority, created_at)
                SELECT u.id, u.domain_id, :svc, 'queued', :reason, MAX(u.priority, :prio), :t FROM urls u
                WHERE {' AND '.join(where)}
                  AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.service = :svc
                                  AND s.state IN ('queued', 'running', 'retry'))
                """,
                params,
            )
            total += cur.rowcount
        if total:
            db.log(con, f"Queued {plural(total, reason + ' submission')}", domain_id)
    return total


def claim_submission(con, service: str, worker_id: str, after_domain: int = 0):
    """Atomically take the next submission for a service.

    Priority items go first; otherwise domains are served round-robin so one huge
    site does not starve the others in the combined queue.
    """
    t = now()
    elig = (
        "s.service = :svc AND d.paused = 0 AND "
        "(s.state = 'queued' OR (s.state = 'retry' AND s.next_attempt_at <= :t))"
    )
    p = {"svc": service, "t": t, "after": after_domain}
    with tx(con):
        top = con.execute(
            f"""SELECT s.id, s.priority, d.priority AS dprio FROM submissions s JOIN domains d ON d.id = s.domain_id
                WHERE {elig} ORDER BY s.priority DESC, d.priority DESC, s.id LIMIT 1""", p
        ).fetchone()
        if not top:
            return None
        sid = top["id"]
        if top["priority"] <= 0 and top["dprio"] <= 0:
            nxt = con.execute(
                f"""SELECT s.id FROM submissions s JOIN domains d ON d.id = s.domain_id
                    WHERE {elig} AND s.domain_id > :after ORDER BY s.domain_id, s.id LIMIT 1""", p
            ).fetchone()
            if nxt:
                sid = nxt["id"]
        con.execute(
            """UPDATE submissions SET state = 'running', worker = ?, lease_until = ?, attempts = attempts + 1,
                   started_at = COALESCE(started_at, ?), last_attempt_at = ?
               WHERE id = ?""",
            (worker_id, t + 3600, t, t, sid),
        )
        return one(
            con,
            """SELECT s.*, u.url, u.final_url, d.host, d.render_js FROM submissions s
               JOIN urls u ON u.id = s.url_id JOIN domains d ON d.id = s.domain_id WHERE s.id = ?""",
            sid,
        )


def save_job_id(con, sub_id: int, job_id: str):
    con.execute("UPDATE submissions SET job_id = ? WHERE id = ?", (job_id, sub_id))


def finish_submission(con, sub, res, settings: dict):
    """Store the outcome of one attempt and schedule a retry when the failure is temporary."""
    t = now()
    max_attempts = int(settings.get("max_attempts", 5))
    base = float(settings.get("retry_base_seconds", 30))
    duration = int((t - (sub["last_attempt_at"] or t)) * 1000)
    outcome = res.outcome
    if outcome == "retry" and sub["attempts"] >= max_attempts:
        outcome = "failed"
        res.error = f"gave up after {sub['attempts']} attempts: {res.error}"
    with tx(con):
        if outcome == "success":
            con.execute(
                """UPDATE submissions SET state = 'success', finished_at = ?, archive_url = ?, archive_id = ?,
                       http_status = ?, error = NULL, note = ?, duration_ms = ?, worker = NULL, lease_until = NULL
                   WHERE id = ?""",
                (t, res.archive_url, res.archive_id, res.http_status, res.note, duration, sub["id"]),
            )
            con.execute("UPDATE domains SET last_submission_at = ? WHERE id = ?", (t, sub["domain_id"]))
            con.execute("UPDATE service_state SET last_ok_at = ? WHERE service = ?", (t, sub["service"]))
            db.log(con, f"{label(sub['service'])}: archived {sub['url']}", sub["domain_id"])
        elif outcome == "retry":
            delay = res.retry_after or min(base * 2 ** (sub["attempts"] - 1), 6 * 3600)
            delay *= random.uniform(0.85, 1.15)
            con.execute(
                """UPDATE submissions SET state = 'retry', next_attempt_at = ?, error = ?, http_status = ?,
                       job_id = ?, duration_ms = ?, worker = NULL, lease_until = NULL WHERE id = ?""",
                (t + delay, res.error, res.http_status, res.job_id, duration, sub["id"]),
            )
            db.log(con, f"{label(sub['service'])}: will retry {sub['url']} in {int(delay)}s ({res.error})",
                   sub["domain_id"], "warn")
        else:  # failed | blocked
            con.execute(
                """UPDATE submissions SET state = ?, finished_at = ?, error = ?, http_status = ?, manual_url = ?,
                       duration_ms = ?, worker = NULL, lease_until = NULL WHERE id = ?""",
                (outcome, t, res.error, res.http_status, res.manual_url, duration, sub["id"]),
            )
            con.execute("UPDATE domains SET last_submission_at = ? WHERE id = ?", (t, sub["domain_id"]))
            db.log(con, f"{label(sub['service'])}: {outcome} {sub['url']} - {res.error}", sub["domain_id"], "error")
        if res.cooldown:
            con.execute(
                "UPDATE service_state SET cooldown_until = ?, last_error = ? WHERE service = ?",
                (t + res.cooldown, res.error, sub["service"]),
            )
            db.log(con, f"{label(sub['service'])} paused for {int(res.cooldown)}s: {res.error}", None, "warn")


def retry_submissions(con, domain_id=None, sub_ids=None, states=("failed", "blocked")) -> int:
    where = [f"state IN ({','.join('?' for _ in states)})"]
    args = list(states)
    if domain_id:
        where.append("domain_id = ?")
        args.append(domain_id)
    if sub_ids:
        where.append(f"id IN ({','.join(str(int(i)) for i in sub_ids)})")
    with tx(con):
        cur = con.execute(
            f"""UPDATE submissions SET state = 'queued', attempts = 0, finished_at = NULL, error = NULL,
                    next_attempt_at = NULL, reason = 'retry' WHERE {' AND '.join(where)}""",
            args,
        )
    return cur.rowcount


def cancel_submissions(con, domain_id=None, sub_ids=None) -> int:
    where = ["state IN ('queued', 'retry')"]
    args = []
    if domain_id:
        where.append("domain_id = ?")
        args.append(domain_id)
    if sub_ids:
        where.append(f"id IN ({','.join(str(int(i)) for i in sub_ids)})")
    with tx(con):
        cur = con.execute(
            f"UPDATE submissions SET state = 'cancelled', finished_at = ? WHERE {' AND '.join(where)}",
            [now(), *args],
        )
    return cur.rowcount


def record_manual(con, sub_id: int, archive_url: str):
    """Record a capture the user made by hand (e.g. after an archive.today CAPTCHA)."""
    with tx(con):
        con.execute(
            """UPDATE submissions SET state = 'success', archive_url = ?, archive_id = ?, finished_at = ?,
                   error = NULL, note = 'recorded manually' WHERE id = ?""",
            (archive_url, archive_url.rstrip("/").rsplit("/", 1)[-1], now(), sub_id),
        )


# --------------------------------------------------------------------------- recovery

def live_workers_sql() -> str:
    return f"SELECT id FROM workers WHERE stopped_at IS NULL AND heartbeat_at >= {now() - HEARTBEAT_STALE}"


def recover(con, only_worker: str | None = None) -> dict:
    """Return work held by dead (or stopping) workers to the queue."""
    t = now()
    if only_worker:
        cond, args = "worker = ?", (only_worker,)
    else:
        cond, args = f"(lease_until < {t} OR worker IS NULL OR worker NOT IN ({live_workers_sql()}))", ()
    with tx(con):
        # a graceful release did not really use up an attempt; a crash does (so a URL
        # that keeps killing workers still ends up in the failed list)
        attempts = "MAX(attempts - 1, 0)" if only_worker else "attempts"
        subs = con.execute(
            f"""UPDATE submissions SET state = 'retry', next_attempt_at = {t}, worker = NULL, lease_until = NULL,
                    attempts = {attempts}, note = 'resumed after interruption'
                WHERE state = 'running' AND {cond}""", args
        ).rowcount
        pages = con.execute(
            f"UPDATE frontier SET state = 'pending', worker = NULL, lease_until = NULL WHERE state = 'leased' AND {cond}",
            args,
        ).rowcount
        scans = con.execute(
            f"UPDATE scans SET worker = NULL, lease_until = NULL WHERE state = 'running' AND worker IS NOT NULL AND {cond}",
            args,
        ).rowcount
        if not only_worker:
            con.execute(
                f"UPDATE workers SET status = 'lost', stopped_at = heartbeat_at "
                f"WHERE stopped_at IS NULL AND heartbeat_at < {t - HEARTBEAT_STALE}"
            )
        if subs or pages or scans:
            what = "released" if only_worker else "recovered from an interrupted worker"
            db.log(con, f"{plural(subs, 'submission')}, {plural(pages, 'page fetch', 'page fetches')} and {plural(scans, 'scan')} {what}", None, "warn")
    return {"submissions": subs, "pages": pages, "scans": scans}


# --------------------------------------------------------------------------- stats

def label(service: str) -> str:
    return {"wayback": "Wayback Machine", "archive_today": "archive.today", "local": "Local snapshot"}.get(service, service)


def submission_counts(con, domain_id=None) -> dict:
    """{service: {state: n}} plus a '_all' roll-up."""
    q = "SELECT service, state, COUNT(*) n FROM submissions"
    args = ()
    if domain_id:
        q += " WHERE domain_id = ?"
        args = (domain_id,)
    out: dict = {"_all": {}}
    for r in con.execute(q + " GROUP BY service, state", args):
        out.setdefault(r["service"], {})[r["state"]] = r["n"]
        out["_all"][r["state"]] = out["_all"].get(r["state"], 0) + r["n"]
    return out


def domain_overview(con) -> list[dict]:
    rows = con.execute(
        """
        SELECT d.*,
          (SELECT COUNT(*) FROM urls u WHERE u.domain_id = d.id) AS urls,
          (SELECT COUNT(*) FROM urls u WHERE u.domain_id = d.id AND u.is_external = 0 AND u.skip_reason IS NULL) AS archivable,
          (SELECT state FROM scans s WHERE s.domain_id = d.id ORDER BY id DESC LIMIT 1) AS scan_state,
          (SELECT number FROM scans s WHERE s.domain_id = d.id ORDER BY id DESC LIMIT 1) AS scan_number
        FROM domains d ORDER BY d.priority DESC, d.host
        """
    ).fetchall()
    counts: dict = {}
    for r in con.execute("SELECT domain_id, state, service, COUNT(*) n FROM submissions GROUP BY domain_id, state, service"):
        c = counts.setdefault(r["domain_id"], {"states": {}, "services": set(), "ok": {}})
        c["states"][r["state"]] = c["states"].get(r["state"], 0) + r["n"]
        c["services"].add(r["service"])
        if r["state"] == "success":
            c["ok"][r["service"]] = c["ok"].get(r["service"], 0) + r["n"]
    out = []
    for r in rows:
        d = dict(r)
        c = counts.get(r["id"], {"states": {}, "services": set(), "ok": {}})
        st = c["states"]
        d["queued"] = st.get("queued", 0) + st.get("retry", 0)
        d["running"] = st.get("running", 0)
        d["success"] = st.get("success", 0)
        d["failed"] = st.get("failed", 0)
        d["blocked"] = st.get("blocked", 0)
        d["submitted"] = sum(v for k, v in st.items() if k in ("success", "failed", "blocked")) + d["running"]
        d["pending"] = d["queued"] + d["running"]
        d["used_services"] = sorted(c["services"])
        d["archived_by_service"] = c["ok"]
        d["status"] = domain_status(d)
        out.append(d)
    return out


def domain_status(d: dict) -> str:
    if d.get("scan_state") == "running":
        return "scanning"
    if d.get("paused") and d.get("pending"):
        return "paused"
    if d.get("running") or d.get("queued"):
        return "archiving"
    if not d.get("scan_number"):
        return "new"
    return "idle"


def throughput(con, window: float = 3600) -> dict:
    t = now()
    pages = scalar(con, "SELECT COUNT(*) FROM frontier WHERE kind = 'page' AND done_at >= ?", t - 60) or 0
    caps = scalar(con, "SELECT COUNT(*) FROM submissions WHERE state = 'success' AND finished_at >= ?", t - window) or 0
    per_service = {
        r["service"]: dict(r)
        for r in con.execute(
            """SELECT service, COUNT(*) n, AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms
               FROM submissions WHERE finished_at >= ? AND state = 'success' GROUP BY service""",
            (t - 24 * 3600,),
        )
    }
    return {"pages_per_min": pages, "captures_per_hour": caps, "services": per_service}


def wait_until_idle(con, timeout=60.0, poll=0.25) -> bool:
    """Test helper: block until nothing is running or queued."""
    end = time.time() + timeout
    while time.time() < end:
        busy = scalar(con, "SELECT COUNT(*) FROM scans WHERE state = 'running'") + scalar(
            con, "SELECT COUNT(*) FROM submissions WHERE state IN ('queued', 'running') OR (state = 'retry' AND next_attempt_at <= ?)",
            now(),
        )
        if not busy:
            return True
        time.sleep(poll)
    return False
