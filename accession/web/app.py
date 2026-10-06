"""Web interface (FastAPI + server-rendered Jinja templates)."""
import asyncio
import csv
import difflib
import hashlib
import hmac
import io
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, render, repo
from ..db import plural
from ..services import REGISTRY
from ..services.local import read_snapshot
from ..urls import normalize

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
ENGINE = None


@asynccontextmanager
async def lifespan(app):
    global ENGINE
    db.init_db(db.conn())
    if os.environ.get("ACCESSION_NO_WORKER") != "1" and not config.SERVERLESS:
        from ..worker import Engine

        ENGINE = Engine()
        ENGINE.start_in_thread()
    yield
    if ENGINE:
        ENGINE.stop()


app = FastAPI(title="Accession", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# --------------------------------------------------------------------------- password protection

SESSION_COOKIE = "accession_session"
SESSION_DAYS = 30
OPEN_PATHS = ("/login", "/static/", "/api/health")


def _session_key() -> bytes:
    return hashlib.sha256(f"accession:{config.SECRET or config.PASSWORD}".encode()).digest()


def _session_token(expires: int) -> str:
    sig = hmac.new(_session_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, _ = token.split(".", 1)
    return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(token, _session_token(int(expires)))


# actions only the owner may take on a public instance
OWNER_ONLY = re.compile(r"^/(system|domains/\d+/delete|services/[^/]+/(toggle|resume)|queue/toggle)$")


def is_owner(request: Request) -> bool:
    return getattr(request.state, "owner", True)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    signed_in = bool(config.PASSWORD) and _valid_session(request.cookies.get(SESSION_COOKIE))
    request.state.owner = signed_in or (not config.PASSWORD and not config.PUBLIC)
    if config.SERVERLESS and not config.PASSWORD and not config.PUBLIC and not path.startswith("/static/"):
        # never serve an unprotected private instance by accident
        return HTMLResponse(templates.get_template("setup.html").render(db_ok=bool(config.DB_URL),
                                                                        ASSET_VERSION=ASSET_VERSION), status_code=503)
    if config.PUBLIC:
        if request.method == "POST" and OWNER_ONLY.match(path) and not signed_in:
            back = request.headers.get("referer") or "/"
            resp = RedirectResponse(f"/login?next={quote(urlsplit(back).path or '/')}", status_code=303)
            resp.set_cookie("flash", quote("Only the owner can do that. Sign in first."), max_age=20)
            return resp
        return await call_next(request)
    if not config.PASSWORD or path.startswith(OPEN_PATHS):
        return await call_next(request)
    cron = config.CRON_SECRET and request.headers.get("authorization") == f"Bearer {config.CRON_SECRET}"
    if (path == "/api/tick" and cron) or _valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"error": "login required"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    flash = unquote(request.cookies.get("flash") or "") or None
    resp = templates.TemplateResponse(request, "login.html", {"next": next, "error": None, "flash": flash})
    if flash:
        resp.delete_cookie("flash")
    return resp


@app.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/")):
    if not config.PASSWORD or not hmac.compare_digest(password.encode(), config.PASSWORD.encode()):
        await asyncio.sleep(1)  # slow down guessing
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": "That is not the password."},
                                          status_code=401)
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(SESSION_COOKIE, _session_token(int(time.time()) + SESSION_DAYS * 86400),
                    max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or config.SERVERLESS)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------------------- serverless worker

@app.api_route("/api/tick", methods=["GET", "POST"])
async def tick():
    """Run one bounded burst of background work (serverless hosts only).

    Open pages call this in a loop, and a Vercel cron job calls it on a schedule, so
    scans and submissions advance without a process that outlives a request.
    """
    if not config.SERVERLESS:
        return {"ran": False, "reason": "a continuous worker is running"}
    seconds = float(os.environ.get("ACCESSION_BURST_SECONDS", 40))
    try:
        from ..worker import Engine

        return await asyncio.to_thread(lambda: asyncio.run(Engine().burst(seconds)))
    except Exception as e:  # report instead of a bare 500; the next tick tries again
        import traceback

        traceback.print_exc()
        return JSONResponse({"ran": False, "reason": "error", "error": f"{type(e).__name__}: {e}"[:500]},
                            status_code=500)


# --------------------------------------------------------------------------- template helpers

def ago(ts):
    if not ts:
        return "—"
    d = time.time() - ts
    if d < 0:
        d = -d
        for unit, sec in (("d", 86400), ("h", 3600), ("min", 60)):
            if d >= sec:
                return f"in {int(d // sec)} {unit}"
        return "in a moment"
    if d < 45:
        return "just now"
    for unit, sec in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            return f"{int(d // sec)} {unit} ago"
    return "just now"


def _display_tz():
    if not config.DISPLAY_TZ:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.DISPLAY_TZ)
    except Exception:
        return None


DISPLAY_TZ = _display_tz()


def dt(ts, fmt="%d %b %Y, %H:%M"):
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime(fmt) if ts else "—"


def num(n):
    return "—" if n is None else f"{int(n):,}"


def dur(seconds):
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 1:
        return f"{int(s * 1000)} ms"
    if s < 90:
        return f"{s:.1f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


def split_url(url):
    p = urlsplit(url or "")
    rest = url[len(f"{p.scheme}://{p.netloc}"):] if p.netloc else url
    return f"{p.scheme}://{p.netloc}", rest or "/"


def svc_label(name):
    return repo.label(name)


templates.env.filters.update(ago=ago, dt=dt, num=num, dur=dur, split_url=split_url, svc=svc_label)
ASSET_VERSION = str(int(max(p.stat().st_mtime for p in (HERE / "static").iterdir())))
templates.env.globals.update(SERVICES=repo.SERVICES, VERSION=config.VERSION, ASSET_VERSION=ASSET_VERSION, svc_label=svc_label,
                             SERVERLESS=config.SERVERLESS, LOGIN=bool(config.PASSWORD), PUBLIC=config.PUBLIC,
                             PUBLIC_MAX_PAGES=config.PUBLIC_MAX_PAGES,
                             today=lambda: datetime.now().strftime("%A, %d %B %Y"))


def con():
    return db.conn()


def page(request: Request, name: str, **ctx):
    c = con()
    flash = unquote(request.cookies.get("flash") or "") or None
    ctx.setdefault("nav", "")
    ctx["engine"] = engine_status(c)
    ctx["flash"] = flash
    ctx["is_owner"] = is_owner(request)
    ctx["request"] = request
    resp = templates.TemplateResponse(request, name, ctx)
    if flash:
        resp.delete_cookie("flash")
    return resp


def go(url: str, message: str | None = None):
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie("flash", quote(message), max_age=20)
    return resp


def engine_status(c) -> dict:
    t = time.time()
    workers = c.execute(
        "SELECT * FROM workers WHERE stopped_at IS NULL AND heartbeat_at >= ? ORDER BY started_at", (t - repo.HEARTBEAT_STALE,)
    ).fetchall()
    running = db.scalar(c, "SELECT COUNT(*) FROM submissions WHERE state = 'running'")
    scanning = db.scalar(c, "SELECT COUNT(*) FROM scans WHERE state = 'running'")
    paused = db.scalar(c, "SELECT value FROM settings WHERE key = 'queue_paused'") == "1"
    return {"workers": len(workers), "running": running, "scanning": scanning, "paused": paused,
            "serverless": config.SERVERLESS}


def domain_or_404(c, domain_id):
    d = db.one(c, "SELECT * FROM domains WHERE id = ?", domain_id)
    if not d:
        raise HTTPException(404, "No such domain")
    return d


def recent_events(c, domain_id=None, limit=40):
    if domain_id:
        return c.execute("SELECT * FROM events WHERE domain_id = ? ORDER BY id DESC LIMIT ?", (domain_id, limit)).fetchall()
    return c.execute(
        "SELECT e.*, d.host FROM events e LEFT JOIN domains d ON d.id = e.domain_id ORDER BY e.id DESC LIMIT ?", (limit,)
    ).fetchall()


def totals(c) -> dict:
    sub = repo.submission_counts(c)["_all"]
    return {
        "domains": db.scalar(c, "SELECT COUNT(*) FROM domains"),
        "urls": db.scalar(c, "SELECT COUNT(*) FROM urls"),
        "queued": sub.get("queued", 0) + sub.get("retry", 0),
        "running": sub.get("running", 0),
        "success": sub.get("success", 0),
        "failed": sub.get("failed", 0),
        "blocked": sub.get("blocked", 0),
        "submitted": sum(sub.get(k, 0) for k in ("success", "failed", "blocked", "running")),
        "pending": sub.get("queued", 0) + sub.get("retry", 0) + sub.get("running", 0),
        "never": db.scalar(
            c,
            """SELECT COUNT(*) FROM urls u WHERE u.skip_reason IS NULL AND u.fetch_error IS NULL
               AND (u.final_status IS NULL OR u.final_status < 400) AND
               NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.state = 'success')""",
        ),
        "last_scan": db.scalar(c, "SELECT MAX(last_scan_at) FROM domains"),
        "last_submission": db.scalar(c, "SELECT MAX(last_submission_at) FROM domains"),
    }


def scan_progress(c, scan) -> dict | None:
    if not scan:
        return None
    rows = {(r["kind"], r["state"]): r["n"] for r in c.execute(
        "SELECT kind, state, COUNT(*) n FROM frontier WHERE scan_id = ? GROUP BY kind, state", (scan["id"],))}
    total = sum(rows.values())
    done = sum(n for (k, s), n in rows.items() if s in ("done", "skipped", "failed"))
    elapsed = (scan["finished_at"] or time.time()) - scan["started_at"]
    return {
        "total": total, "done": done, "pct": (100 * done / total) if total else 0,
        "pending": sum(n for (k, s), n in rows.items() if s in ("pending", "leased")),
        "elapsed": elapsed,
        "rate": scan["pages_fetched"] / elapsed * 60 if elapsed > 1 else 0,
    }


# --------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    c = con()
    domains = repo.domain_overview(c)
    running = {r["domain_id"]: r for r in c.execute("SELECT * FROM scans WHERE state = 'running'")}
    progress = {did: scan_progress(c, s) for did, s in running.items()}
    services = service_health(c)
    latest = c.execute(
        """SELECT s.id, s.service, s.archive_url, s.finished_at, s.url_id, u.url, d.host
           FROM submissions s JOIN urls u ON u.id = s.url_id JOIN domains d ON d.id = s.domain_id
           WHERE s.state = 'success' AND s.archive_url IS NOT NULL
           ORDER BY s.finished_at DESC LIMIT 12"""
    ).fetchall()
    access, secret = config.ia_keys(db.settings(c))
    needs_keys = not (access and secret) and any("wayback" in (d["services"] or "") for d in domains)
    return page(
        request, "dashboard.html", nav="register", domains=domains, totals=totals(c), progress=progress,
        events=recent_events(c, limit=30), tp=repo.throughput(c), services=services,
        render_available=render.available(), latest=latest, needs_keys=needs_keys,
    )


def service_health(c) -> list[dict]:
    s = db.settings(c)
    out = []
    counts = repo.submission_counts(c)
    tp = repo.throughput(c)["services"]
    for name, cls in REGISTRY.items():
        st = db.one(c, "SELECT * FROM service_state WHERE service = ?", name)
        svc = cls(None)
        conc, interval = svc.limits(s)
        ok, mode = svc.configured(s)
        cooling = st and (st["cooldown_until"] or 0) > time.time()
        out.append({
            "name": name, "label": cls.label, "description": cls.description, "mode": mode,
            "enabled": bool(st["enabled"]) if st else True,
            "cooldown_until": st["cooldown_until"] if cooling else None,
            "last_error": st["last_error"] if st else None,
            "last_ok_at": st["last_ok_at"] if st else None,
            "concurrency": conc, "interval": interval,
            "counts": counts.get(name, {}),
            "avg_ms": (tp.get(name) or {}).get("avg_ms"),
            "max_ms": (tp.get(name) or {}).get("max_ms"),
        })
    return out


@app.post("/domains")
def add_domains(
    request: Request,
    domains: str = Form(...),
    services: list[str] = Form(default=[]),
    max_pages: int = Form(5000),
    max_depth: int = Form(12),
    include_subdomains: bool = Form(False),
    external_mode: str = Form("ignore"),
    render_js: bool = Form(False),
    auto_archive: bool = Form(False),
    rescan_hours: str = Form(""),
    start: bool = Form(False),
):
    c = con()
    services = [s for s in services if s in repo.SERVICES] or ["wayback"]
    limited = config.PUBLIC and not is_owner(request)
    if limited:
        max_pages = min(max_pages, config.PUBLIC_MAX_PAGES)
        if rescan_hours.strip():
            rescan_hours = str(max(float(rescan_hours), 24))
    added, existing, bad = [], [], []
    for line in re.split(r"[\s,]+", domains):
        if not line.strip():
            continue
        if config.PUBLIC:
            from ..fetch import is_public_host
            from ..urls import parse_domain_input

            parsed = parse_domain_input(line)
            if parsed and not is_public_host(urlsplit(parsed[1]).hostname or ""):
                bad.append(f"{line} (private address)")
                continue
            if limited and db.scalar(c, "SELECT COUNT(*) FROM domains") >= config.PUBLIC_MAX_DOMAINS:
                bad.append(f"{line} (the register is full)")
                continue
        did, created = repo.add_domain(
            c, line,
            services=",".join(services), max_pages=max(1, max_pages), max_depth=max(0, max_depth),
            include_subdomains=int(include_subdomains), external_mode=external_mode if external_mode in ("ignore", "record", "archive") else "ignore",
            render_js=int(render_js), auto_archive=int(auto_archive),
            rescan_hours=float(rescan_hours) if rescan_hours.strip() else None,
        )
        if did is None:
            bad.append(line)
            continue
        (added if created else existing).append(did)
        if start and created:
            repo.start_scan(c, did)
    parts = []
    if added:
        parts.append((f"Added {plural(len(added), 'domain')}" + (", scanning now" if start else "")))
    if existing:
        parts.append(f"{plural(len(existing), 'domain was', 'domains were')} already in the register")
    if bad:
        parts.append(f"skipped {', '.join(bad[:5])}")
    if len(added) == 1 and not existing:
        return go(f"/domains/{added[0]}", "; ".join(parts))
    return go("/", "; ".join(parts) or "Nothing to add")


# --------------------------------------------------------------------------- domain

INV_PER_PAGE = 100


def inventory_query(domain_id, f: dict):
    where, args = ["u.domain_id = ?"], [domain_id]
    if f.get("q"):
        where.append("u.url LIKE ?")
        args.append(f"%{f['q']}%")
    if f.get("source"):
        where.append("EXISTS (SELECT 1 FROM url_sources us WHERE us.url_id = u.id AND us.source = ?)")
        args.append(f["source"])
    http = f.get("http")
    if http in ("2", "3", "4", "5"):
        where.append("u.http_status BETWEEN ? AND ?")
        args += [int(http) * 100, int(http) * 100 + 99]
    elif http == "none":
        where.append("u.http_status IS NULL AND u.fetch_error IS NULL")
    elif http == "error":
        where.append("u.fetch_error IS NOT NULL")
    svc = f.get("service") or None
    svc_sql = "AND s.service = ?" if svc else ""
    svc_args = [svc] if svc else []
    st = f.get("archive")
    if st == "archived":
        where.append(f"EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.state = 'success' {svc_sql})")
        args += svc_args
    elif st == "never":
        where.append("u.skip_reason IS NULL")
        where.append(f"NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.state = 'success' {svc_sql})")
        args += svc_args
    elif st in ("queued", "failed", "blocked"):
        states = ("queued", "retry", "running") if st == "queued" else (st,)
        where.append(
            f"EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.state IN ({','.join('?' * len(states))}) {svc_sql})")
        args += [*states, *svc_args]
    elif st == "skipped":
        where.append("u.skip_reason IS NOT NULL")
    if f.get("scan"):
        where.append("u.first_scan_id = ?")
        args.append(int(f["scan"]))
    if f.get("seen") == "missing":
        where.append("u.is_external = 0 AND u.last_scan_id < (SELECT MAX(id) FROM scans WHERE domain_id = u.domain_id AND state = 'done')")
    elif f.get("seen") == "changed":
        where.append("u.changed_at IS NOT NULL")
    elif f.get("seen") == "external":
        where.append("u.is_external = 1")
    return " AND ".join(where), args


def latest_by_service(c, url_ids) -> dict:
    if not url_ids:
        return {}
    out: dict = {}
    marks = ",".join("?" * len(url_ids))
    for r in c.execute(
        f"""SELECT url_id, service, state, archive_url, finished_at, error, manual_url,
                   (SELECT COUNT(*) FROM submissions s2 WHERE s2.url_id = s.url_id AND s2.service = s.service AND s2.state = 'success') AS ok_count
            FROM submissions s WHERE url_id IN ({marks}) ORDER BY id""",
        url_ids,
    ):
        cell = out.setdefault(r["url_id"], {})
        prev = cell.get(r["service"])
        # show the newest attempt, but keep pointing at the last good archive link
        good = r if r["state"] == "success" else (prev or {}).get("good")
        cell[r["service"]] = {**dict(r), "good": good}
    return out


@app.get("/domains/{domain_id}", response_class=HTMLResponse)
def domain_view(request: Request, domain_id: int, tab: str = "inventory", p: int = 1):
    c = con()
    d = domain_or_404(c, domain_id)
    f = {k: request.query_params.get(k, "") for k in ("q", "source", "http", "archive", "service", "scan", "seen")}
    overview = next((x for x in repo.domain_overview(c) if x["id"] == domain_id), {})
    scans = c.execute("SELECT * FROM scans WHERE domain_id = ? ORDER BY id DESC", (domain_id,)).fetchall()
    current = next((s for s in scans if s["state"] == "running"), None)
    ctx = dict(
        nav="register", d=d, ov=overview, tab=tab, f=f, scans=scans, current=current,
        progress=scan_progress(c, current) if current else None,
        counts=repo.submission_counts(c, domain_id), services=repo.domain_services(d),
        sources=[r[0] for r in c.execute(
            "SELECT DISTINCT us.source FROM url_sources us JOIN urls u ON u.id = us.url_id WHERE u.domain_id = ? ORDER BY 1",
            (domain_id,))],
        events=recent_events(c, domain_id, 25), render_available=render.available(),
        filters_qs=urlencode({k: v for k, v in f.items() if v}),
    )
    if tab == "inventory":
        where, args = inventory_query(domain_id, f)
        total = db.scalar(c, f"SELECT COUNT(*) FROM urls u WHERE {where}", *args)
        pages = max(1, math.ceil(total / INV_PER_PAGE))
        p = min(max(1, p), pages)
        rows = c.execute(
            f"""SELECT u.*, (SELECT number FROM scans WHERE id = u.first_scan_id) AS scan_no
                FROM urls u WHERE {where} ORDER BY u.is_external, COALESCE(u.depth, 99), u.url
                LIMIT ? OFFSET ?""",
            (*args, INV_PER_PAGE, (p - 1) * INV_PER_PAGE),
        ).fetchall()
        ctx.update(rows=rows, total=total, p=p, pages=pages, offset=(p - 1) * INV_PER_PAGE,
                   subs=latest_by_service(c, [r["id"] for r in rows]))
    elif tab == "history":
        state = request.query_params.get("state", "")
        where, args = "s.domain_id = ?", [domain_id]
        if state:
            where += " AND s.state = ?"
            args.append(state)
        total = db.scalar(c, f"SELECT COUNT(*) FROM submissions s WHERE {where}", *args)
        pages = max(1, math.ceil(total / INV_PER_PAGE))
        p = min(max(1, p), pages)
        ctx.update(
            history=c.execute(
                f"""SELECT s.*, u.url FROM submissions s JOIN urls u ON u.id = s.url_id WHERE {where}
                    ORDER BY COALESCE(s.finished_at, s.last_attempt_at, s.created_at) DESC, s.id DESC LIMIT ? OFFSET ?""",
                (*args, INV_PER_PAGE, (p - 1) * INV_PER_PAGE),
            ).fetchall(),
            total=total, p=p, pages=pages, state=state,
        )
    return page(request, "domain.html", **ctx)


@app.post("/domains/{domain_id}/scan")
def domain_scan(domain_id: int):
    c = con()
    domain_or_404(c, domain_id)
    sid = repo.start_scan(c, domain_id)
    return go(f"/domains/{domain_id}", "Scan started" if sid else "A scan is already running")


@app.post("/domains/{domain_id}/cancel")
def domain_cancel(domain_id: int):
    repo.cancel_scan(con(), domain_id)
    return go(f"/domains/{domain_id}", "Scan stopped. URLs found so far are kept.")


@app.post("/domains/{domain_id}/archive")
def domain_archive(domain_id: int, services: list[str] = Form(default=[])):
    c = con()
    d = domain_or_404(c, domain_id)
    services = [s for s in services if s in repo.SERVICES] or repo.domain_services(d)
    n = repo.enqueue_new(c, domain_id, services)
    msg = f"{plural(n, 'submission')} queued" if n else "Nothing new to submit - every URL already has a submission"
    return go(f"/domains/{domain_id}", msg)


@app.post("/domains/{domain_id}/rearchive")
def domain_rearchive(domain_id: int, services: list[str] = Form(default=[]), scope: str = Form("all")):
    c = con()
    d = domain_or_404(c, domain_id)
    services = [s for s in services if s in repo.SERVICES] or repo.domain_services(d)
    if scope == "changed":
        last = db.one(c, "SELECT * FROM scans WHERE domain_id = ? AND state = 'done' ORDER BY id DESC LIMIT 1", domain_id)
        n = repo.enqueue_rearchive(c, domain_id, services, changed_since=last["started_at"] if last else 0, reason="changed")
    else:
        n = repo.enqueue_rearchive(c, domain_id, services)
    return go(f"/domains/{domain_id}", f"{plural(n, 'fresh snapshot')} requested")


@app.post("/domains/{domain_id}/pause")
def domain_pause(domain_id: int):
    c = con()
    d = domain_or_404(c, domain_id)
    c.execute("UPDATE domains SET paused = ? WHERE id = ?", (0 if d["paused"] else 1, domain_id))
    db.log(c, "Submissions resumed" if d["paused"] else "Submissions paused", domain_id)
    return go(f"/domains/{domain_id}", "Resumed" if d["paused"] else "Paused - nothing from this domain will be submitted")


@app.post("/domains/{domain_id}/retry")
def domain_retry(domain_id: int):
    n = repo.retry_submissions(con(), domain_id=domain_id)
    return go(f"/domains/{domain_id}?tab=history", f"{plural(n, 'submission')} back in the queue")


@app.post("/domains/{domain_id}/clear")
def domain_clear(domain_id: int):
    n = repo.cancel_submissions(con(), domain_id=domain_id)
    return go(f"/domains/{domain_id}", f"{plural(n, 'waiting submission')} cancelled")


@app.post("/domains/{domain_id}/settings")
def domain_settings(
    request: Request,
    domain_id: int,
    services: list[str] = Form(default=[]),
    max_pages: int = Form(...),
    max_depth: int = Form(...),
    crawl_concurrency: int = Form(4),
    include_subdomains: bool = Form(False),
    external_mode: str = Form("ignore"),
    render_js: bool = Form(False),
    respect_robots: bool = Form(False),
    auto_archive: bool = Form(False),
    rearchive_on_change: bool = Form(False),
    rescan_hours: str = Form(""),
    priority: int = Form(0),
):
    c = con()
    domain_or_404(c, domain_id)
    rh = float(rescan_hours) if rescan_hours.strip() else None
    if config.PUBLIC and not is_owner(request):
        max_pages = min(max_pages, config.PUBLIC_MAX_PAGES)
        rh = max(rh, 24) if rh else None
    c.execute(
        """UPDATE domains SET services = ?, max_pages = ?, max_depth = ?, crawl_concurrency = ?, include_subdomains = ?,
               external_mode = ?, render_js = ?, respect_robots = ?, auto_archive = ?, rearchive_on_change = ?,
               rescan_hours = ?, next_scan_at = CASE WHEN ? IS NULL THEN NULL ELSE COALESCE(last_scan_at, ?) + ? * 3600 END,
               priority = ? WHERE id = ?""",
        (
            ",".join(s for s in services if s in repo.SERVICES) or "wayback", max(1, max_pages), max(0, max_depth),
            min(max(1, crawl_concurrency), 32), int(include_subdomains),
            external_mode if external_mode in ("ignore", "record", "archive") else "ignore",
            int(render_js), int(respect_robots), int(auto_archive), int(rearchive_on_change),
            rh, rh, time.time(), rh, priority, domain_id,
        ),
    )
    db.log(c, "Settings updated", domain_id)
    return go(f"/domains/{domain_id}?tab=settings", "Settings saved")


@app.post("/domains/{domain_id}/delete")
def domain_delete(domain_id: int, confirm: str = Form("")):
    c = con()
    d = domain_or_404(c, domain_id)
    if confirm.strip() != d["host"]:
        return go(f"/domains/{domain_id}?tab=settings", "Type the domain name exactly to delete it")
    repo.delete_domain(c, domain_id)
    return go("/", f"{d['host']} and its records were removed")


# --------------------------------------------------------------------------- url

@app.get("/urls/{url_id}", response_class=HTMLResponse)
def url_view(request: Request, url_id: int):
    c = con()
    u = db.one(c, "SELECT * FROM urls WHERE id = ?", url_id)
    if not u:
        raise HTTPException(404, "No such URL")
    d = db.one(c, "SELECT * FROM domains WHERE id = ?", u["domain_id"])
    return page(
        request, "url.html", nav="register", u=u, d=d,
        sources=c.execute("SELECT * FROM url_sources WHERE url_id = ? ORDER BY seen_at", (url_id,)).fetchall(),
        subs=c.execute("SELECT * FROM submissions WHERE url_id = ? ORDER BY id DESC", (url_id,)).fetchall(),
        snaps=c.execute(
            """SELECT id, url_id, submission_id, captured_at, http_status, content_hash, size, path, rendered, title
               FROM snapshots WHERE url_id = ? ORDER BY id DESC""", (url_id,)).fetchall(),
        first_scan=db.one(c, "SELECT * FROM scans WHERE id = ?", u["first_scan_id"]),
        last_scan=db.one(c, "SELECT * FROM scans WHERE id = ?", u["last_scan_id"]),
        browse={name: cls.browse_url(u["url"]) for name, cls in REGISTRY.items()},
    )


@app.post("/urls/{url_id}/archive")
def url_archive(url_id: int, services: list[str] = Form(default=[]), priority: bool = Form(False)):
    c = con()
    u = db.one(c, "SELECT * FROM urls WHERE id = ?", url_id)
    if not u:
        raise HTTPException(404)
    d = db.one(c, "SELECT * FROM domains WHERE id = ?", u["domain_id"])
    services = [s for s in services if s in repo.SERVICES] or repo.domain_services(d)
    if u["skip_reason"]:
        # an explicit request overrides the automatic exclusion
        c.execute("UPDATE urls SET skip_reason = NULL WHERE id = ?", (url_id,))
    n = repo.enqueue_rearchive(c, u["domain_id"], services, url_ids=[url_id], reason="manual",
                               priority=10 if priority else 0)
    if u["skip_reason"]:
        c.execute("UPDATE urls SET skip_reason = ? WHERE id = ?", (u["skip_reason"], url_id))
    return go(f"/urls/{url_id}", f"{plural(n, 'submission')} queued" + (" at the front of the queue" if priority and n else ""))


@app.post("/urls/{url_id}/priority")
def url_priority(url_id: int):
    c = con()
    u = db.one(c, "SELECT * FROM urls WHERE id = ?", url_id)
    new = 0 if u["priority"] > 0 else 10
    c.execute("UPDATE urls SET priority = ? WHERE id = ?", (new, url_id))
    c.execute("UPDATE submissions SET priority = ? WHERE url_id = ? AND state IN ('queued', 'retry')", (new, url_id))
    return go(f"/urls/{url_id}", "Marked as important - it jumps the queue" if new else "Priority cleared")


# --------------------------------------------------------------------------- submissions

@app.post("/submissions/{sub_id}/retry")
def sub_retry(request: Request, sub_id: int):
    repo.retry_submissions(con(), sub_ids=[sub_id], states=("failed", "blocked", "retry", "cancelled"))
    return go(request.headers.get("referer") or "/queue", "Back in the queue")


@app.post("/submissions/{sub_id}/cancel")
def sub_cancel(request: Request, sub_id: int):
    repo.cancel_submissions(con(), sub_ids=[sub_id])
    return go(request.headers.get("referer") or "/queue", "Cancelled")


@app.post("/submissions/{sub_id}/manual")
def sub_manual(request: Request, sub_id: int, archive_url: str = Form(...)):
    archive_url = archive_url.strip()
    if not re.match(r"^https?://\S+$", archive_url):
        return go(request.headers.get("referer") or "/queue", "That does not look like an archive link")
    repo.record_manual(con(), sub_id, archive_url)
    return go(request.headers.get("referer") or "/queue", "Archive link recorded")


# --------------------------------------------------------------------------- queue

@app.get("/queue", response_class=HTMLResponse)
def queue_view(request: Request):
    c = con()
    t = time.time()
    base = """SELECT s.*, u.url, d.host FROM submissions s JOIN urls u ON u.id = s.url_id
              JOIN domains d ON d.id = s.domain_id"""
    return page(
        request, "queue.html", nav="queue",
        totals=totals(c), services=service_health(c), tp=repo.throughput(c),
        running=c.execute(base + " WHERE s.state = 'running' ORDER BY s.last_attempt_at").fetchall(),
        upcoming=c.execute(base + " WHERE s.state = 'queued' AND d.paused = 0 ORDER BY s.priority DESC, s.id LIMIT 25").fetchall(),
        retrying=c.execute(base + " WHERE s.state = 'retry' ORDER BY s.next_attempt_at LIMIT 50").fetchall(),
        review=c.execute(base + " WHERE s.state IN ('failed', 'blocked') ORDER BY s.finished_at DESC LIMIT 200").fetchall(),
        review_total=db.scalar(c, "SELECT COUNT(*) FROM submissions WHERE state IN ('failed', 'blocked')"),
        per_domain=c.execute(
            """SELECT d.id, d.host, d.paused, SUM(s.state = 'queued') q, SUM(s.state = 'retry') r,
                      SUM(s.state = 'running') run FROM submissions s JOIN domains d ON d.id = s.domain_id
               WHERE s.state IN ('queued', 'retry', 'running') GROUP BY d.id ORDER BY d.host"""
        ).fetchall(),
        workers=c.execute("SELECT * FROM workers ORDER BY started_at DESC LIMIT 12").fetchall(),
        now=t, stale=repo.HEARTBEAT_STALE,
        slow_urls=c.execute(
            """SELECT u.id, u.url, u.response_ms, d.host FROM urls u JOIN domains d ON d.id = u.domain_id
               WHERE u.response_ms IS NOT NULL ORDER BY u.response_ms DESC LIMIT 8"""
        ).fetchall(),
        slow_subs=c.execute(
            """SELECT s.id, s.service, s.duration_ms, u.url, u.id AS url_id FROM submissions s JOIN urls u ON u.id = s.url_id
               WHERE s.state = 'success' AND s.duration_ms IS NOT NULL ORDER BY s.duration_ms DESC LIMIT 8"""
        ).fetchall(),
    )


@app.post("/queue/toggle")
def queue_toggle():
    c = con()
    paused = db.scalar(c, "SELECT value FROM settings WHERE key = 'queue_paused'") == "1"
    db.set_setting(c, "queue_paused", "0" if paused else "1")
    db.log(c, "Queue resumed" if paused else "Queue paused - running submissions finish, nothing new starts", None, "warn")
    return go("/queue", "Queue resumed" if paused else "Queue paused")


@app.post("/queue/retry-all")
def queue_retry_all():
    n = repo.retry_submissions(con())
    return go("/queue", f"{plural(n, 'submission')} back in the queue")


@app.post("/services/{name}/toggle")
def service_toggle(name: str):
    c = con()
    st = db.one(c, "SELECT * FROM service_state WHERE service = ?", name)
    if not st:
        raise HTTPException(404)
    c.execute("UPDATE service_state SET enabled = ?, cooldown_until = NULL WHERE service = ?", (0 if st["enabled"] else 1, name))
    return go("/queue", f"{repo.label(name)} {'disabled' if st['enabled'] else 'enabled'}")


@app.post("/services/{name}/resume")
def service_resume(name: str):
    con().execute("UPDATE service_state SET cooldown_until = NULL WHERE service = ?", (name,))
    return go("/queue", f"{repo.label(name)} cooldown cleared")


# --------------------------------------------------------------------------- search

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", service: str = "", state: str = "", domain: str = "", p: int = 1):
    c = con()
    results, total, exact = [], 0, None
    q = q.strip()
    if q or service or state or domain:
        where, args = ["1"], []
        if q:
            norm = normalize(q if "://" in q else "https://" + q) if "." in q and " " not in q else None
            if norm and "/" in q.split("://")[-1]:
                exact = db.one(c, "SELECT * FROM urls WHERE url = ? OR original_url = ? OR url = ?",
                               norm, q, norm.replace("https://", "http://", 1))
            where.append("(u.url LIKE ? OR u.original_url LIKE ? OR d.host LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if domain:
            where.append("d.id = ?")
            args.append(int(domain))
        if service or state:
            sw, sa = [], []
            if service:
                sw.append("s.service = ?")
                sa.append(service)
            if state == "never":
                where.append(f"NOT EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND s.state = 'success'"
                             f"{' AND ' + sw[0] if sw else ''})")
                args += sa
            elif state:
                sw.append("s.state IN (" + ("'queued','retry','running'" if state == "queued" else "?") + ")")
                if state != "queued":
                    sa.append(state)
                where.append(f"EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND {' AND '.join(sw)})")
                args += sa
            else:
                where.append(f"EXISTS (SELECT 1 FROM submissions s WHERE s.url_id = u.id AND {sw[0]})")
                args += sa
        sql_where = " AND ".join(where)
        total = db.scalar(c, f"SELECT COUNT(*) FROM urls u JOIN domains d ON d.id = u.domain_id WHERE {sql_where}", *args)
        pages = max(1, math.ceil(total / INV_PER_PAGE))
        p = min(max(1, p), pages)
        results = c.execute(
            f"""SELECT u.*, d.host FROM urls u JOIN domains d ON d.id = u.domain_id WHERE {sql_where}
                ORDER BY d.host, u.url LIMIT ? OFFSET ?""",
            (*args, INV_PER_PAGE, (p - 1) * INV_PER_PAGE),
        ).fetchall()
    else:
        pages = 1
    subs = latest_by_service(c, [r["id"] for r in results] + ([exact["id"]] if exact else []))
    exact_history = c.execute("SELECT * FROM submissions WHERE url_id = ? ORDER BY id DESC", (exact["id"],)).fetchall() if exact else []
    return page(
        request, "search.html", nav="search", q=q, service=service, state=state, domain=domain,
        results=results, total=total, p=p, pages=pages, subs=subs, exact=exact, exact_history=exact_history,
        domains=c.execute("SELECT id, host FROM domains ORDER BY host").fetchall(),
        qs=urlencode({k: v for k, v in dict(q=q, service=service, state=state, domain=domain).items() if v}),
    )


# --------------------------------------------------------------------------- snapshots

@app.get("/snapshots/{snap_id}")
def snapshot(snap_id: int, raw: int = 0):
    c = con()
    s = db.one(c, "SELECT s.*, u.url FROM snapshots s JOIN urls u ON u.id = s.url_id WHERE s.id = ?", snap_id)
    if not s:
        raise HTTPException(404)
    body = read_snapshot(s)
    if raw:
        return Response(body, media_type="text/plain; charset=utf-8")
    html = body.decode("utf-8", "replace")
    banner = (
        f'<base href="{s["url"]}"><div style="all:initial;display:block;font:13px/1.4 ui-monospace,Menlo,monospace;'
        f'background:#1f1d1a;color:#f5f2eb;padding:8px 14px;position:sticky;top:0;z-index:2147483647">'
        f'Accession local snapshot #{s["id"]} &middot; {s["url"]} &middot; captured {dt(s["captured_at"])}'
        f' &middot; sha256 {s["content_hash"][:16]} &middot; scripts disabled</div>'
    )
    html = re.sub(r"(<head[^>]*>)", r"\1" + banner.replace("\\", "\\\\"), html, count=1, flags=re.I) if re.search(r"<head", html, re.I) else banner + html
    # sandbox: the archived page cannot run scripts or reach back into this app
    headers = {"Content-Security-Policy": "sandbox; script-src 'none'; object-src 'none'"}
    return HTMLResponse(html, headers=headers)


def _text_lines(html: str) -> list[str]:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article|header|footer)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    import html as h

    lines = [" ".join(h.unescape(line).split()) for line in text.splitlines()]
    return [ln for ln in lines if ln]


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, a: int, b: int, mode: str = "text"):
    c = con()
    sa = db.one(c, "SELECT s.*, u.url FROM snapshots s JOIN urls u ON u.id = s.url_id WHERE s.id = ?", a)
    sb = db.one(c, "SELECT s.*, u.url FROM snapshots s JOIN urls u ON u.id = s.url_id WHERE s.id = ?", b)
    if not sa or not sb:
        raise HTTPException(404)
    ba, bb = read_snapshot(sa).decode("utf-8", "replace"), read_snapshot(sb).decode("utf-8", "replace")
    if mode == "html":
        la, lb = ba.splitlines(), bb.splitlines()
    else:
        la, lb = _text_lines(ba), _text_lines(bb)
    diff = list(difflib.unified_diff(la, lb, f"#{a}", f"#{b}", lineterm="", n=3))[2:]
    added = sum(1 for x in diff if x.startswith("+"))
    removed = sum(1 for x in diff if x.startswith("-"))
    ratio = difflib.SequenceMatcher(None, la, lb).ratio() if len(la) + len(lb) < 20000 else None
    u = db.one(c, "SELECT * FROM urls WHERE id = ?", sa["url_id"])
    return page(request, "compare.html", nav="register", a=sa, b=sb, diff=diff, added=added, removed=removed,
                ratio=ratio, mode=mode, u=u, same=sa["content_hash"] == sb["content_hash"])


# --------------------------------------------------------------------------- system

SETTING_FIELDS = [
    ("max_parallel_scans", "Scans running at once", "int"),
    ("max_attempts", "Attempts before a submission is marked failed", "int"),
    ("retry_base_seconds", "First retry delay (doubles each attempt), seconds", "int"),
    ("fetch_timeout", "Page fetch timeout, seconds", "int"),
    ("wayback_concurrency", "Wayback: parallel captures", "int"),
    ("wayback_interval", "Wayback: seconds between captures (anonymous)", "float"),
    ("wayback_interval_auth", "Wayback: seconds between captures (with keys)", "float"),
    ("wayback_reuse_days", "Wayback: reuse an existing capture younger than N days (0 = off)", "float"),
    ("wayback_poll_timeout", "Wayback: give up polling a job after, seconds", "int"),
    ("archive_today_host", "archive.today mirror", "str"),
    ("archive_today_concurrency", "archive.today: parallel submissions", "int"),
    ("archive_today_interval", "archive.today: seconds between submissions", "float"),
    ("local_concurrency", "Local snapshots: parallel captures", "int"),
    ("local_interval", "Local snapshots: seconds between captures", "float"),
]


@app.get("/system", response_class=HTMLResponse)
def system(request: Request):
    c = con()
    s = db.settings(c)
    size = sum(p.stat().st_size for p in config.HOME.glob("accession.db*") if p.exists())
    snap_size = (db.scalar(c, "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM snapshots") or 0) + (
        sum(p.stat().st_size for p in config.SNAPSHOT_DIR.rglob("*.gz")) if config.SNAPSHOT_DIR.exists() else 0)
    access, secret = config.ia_keys(s)
    return page(
        request, "system.html", nav="system", s=s, fields=SETTING_FIELDS, services=service_health(c),
        db_path=config.DB_PATH, db_size=size, snap_dir=config.SNAPSHOT_DIR, snap_size=snap_size,
        has_keys=bool(access and secret), keys_from_env=bool(os.environ.get("IA_ACCESS_KEY")),
        render_available=render.available(),
        counts={t: db.scalar(c, f"SELECT COUNT(*) FROM {t}") for t in ("domains", "scans", "urls", "url_sources", "submissions", "snapshots", "events")},
        workers=c.execute("SELECT * FROM workers ORDER BY started_at DESC LIMIT 8").fetchall(), now=time.time(),
        stale=repo.HEARTBEAT_STALE,
    )


@app.post("/system")
async def system_save(request: Request):
    form = await request.form()
    c = con()
    for key, _, kind in SETTING_FIELDS:
        if key in form:
            v = str(form[key]).strip()
            try:
                v = str(int(float(v))) if kind == "int" else str(float(v)) if kind == "float" else v
            except ValueError:
                continue
            db.set_setting(c, key, v)
    if form.get("ia_access_key", "").strip():
        db.set_setting(c, "ia_access_key", form["ia_access_key"].strip())
    if form.get("ia_secret_key", "").strip():
        db.set_setting(c, "ia_secret_key", form["ia_secret_key"].strip())
    if form.get("clear_keys"):
        db.set_setting(c, "ia_access_key", "")
        db.set_setting(c, "ia_secret_key", "")
    return go("/system", "Settings saved")


# --------------------------------------------------------------------------- export

EXPORT_COLS = [
    "domain", "url_id", "original_url", "normalized_url", "discovery_source", "found_on", "discovered_at",
    "first_scan", "last_seen_at", "http_status", "final_status", "final_url", "content_type", "skip_reason",
    "service", "submission_state", "reason", "submitted_at", "completed_at", "last_attempted_at", "attempts",
    "archive_url", "archive_id", "error",
]


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


def export_rows(c, domain_id=None):
    where = "WHERE u.domain_id = ?" if domain_id else ""
    args = (domain_id,) if domain_id else ()
    cur = c.execute(
        f"""SELECT d.host, u.*, (SELECT number FROM scans WHERE id = u.first_scan_id) scan_no,
                   s.service, s.state, s.reason, s.started_at s_started, s.finished_at s_finished,
                   s.last_attempt_at s_last, s.attempts s_attempts, s.archive_url, s.archive_id, s.error s_error
            FROM urls u JOIN domains d ON d.id = u.domain_id
            LEFT JOIN submissions s ON s.url_id = u.id {where} ORDER BY d.host, u.id, s.id""",
        args,
    )
    for r in cur:
        yield {
            "domain": r["host"], "url_id": r["id"], "original_url": r["original_url"], "normalized_url": r["url"],
            "discovery_source": r["source"], "found_on": r["found_on"] or "", "discovered_at": _iso(r["first_seen_at"]),
            "first_scan": r["scan_no"] or "", "last_seen_at": _iso(r["last_seen_at"]),
            "http_status": r["http_status"] or "", "final_status": r["final_status"] or "", "final_url": r["final_url"] or "",
            "content_type": r["content_type"] or "", "skip_reason": r["skip_reason"] or "",
            "service": r["service"] or "", "submission_state": r["state"] or "", "reason": r["reason"] or "",
            "submitted_at": _iso(r["s_started"]), "completed_at": _iso(r["s_finished"]),
            "last_attempted_at": _iso(r["s_last"]), "attempts": r["s_attempts"] if r["s_attempts"] is not None else "",
            "archive_url": r["archive_url"] or "", "archive_id": r["archive_id"] or "", "error": r["s_error"] or "",
        }


@app.get("/export/{target}.{fmt}")
def export(target: str, fmt: str):
    c = db.connect()  # own connection: the response streams after this function returns
    domain_id = None
    name = "accession-all"
    if target != "all":
        d = domain_or_404(c, int(target))
        domain_id, name = d["id"], f"accession-{d['host'].replace(':', '_')}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if fmt == "csv":
        def gen():
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=EXPORT_COLS)
            w.writeheader()
            for i, row in enumerate(export_rows(c, domain_id)):
                w.writerow(row)
                if i % 500 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'})
    if fmt == "json":
        def gen_json():
            yield '{"generated_at": "%s", "urls": [\n' % datetime.now().isoformat(timespec="seconds")
            current, first = None, True
            for row in export_rows(c, domain_id):
                if current and current["url_id"] != row["url_id"]:
                    yield ("" if first else ",\n") + json.dumps(current)
                    first = False
                    current = None
                if current is None:
                    current = {k: row[k] for k in EXPORT_COLS[:14]}
                    current["submissions"] = []
                if row["service"]:
                    current["submissions"].append({k: row[k] for k in EXPORT_COLS[14:]})
            if current:
                yield ("" if first else ",\n") + json.dumps(current)
            yield "\n]}\n"

        return StreamingResponse(gen_json(), media_type="application/json",
                                 headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.json"'})
    raise HTTPException(404)


# --------------------------------------------------------------------------- live JSON

def _log_html(events, show_host=True):
    return templates.get_template("_log.html").render(events=events, show_host=show_host)


@app.get("/api/live")
def live(domain: int | None = None):
    """Values the open page swaps in place every couple of seconds."""
    c = con()
    k, st, bar = {}, {}, {}
    eng = engine_status(c)
    k["engine"] = engine_line(eng)
    st["engine"] = "ok" if eng["workers"] or eng["serverless"] else "down"
    if domain is None:
        tot = totals(c)
        for key in ("domains", "urls", "queued", "submitted", "success", "failed", "blocked", "pending", "never"):
            k[f"t-{key}"] = num(tot[key])
        k["t-last_scan"] = ago(tot["last_scan"])
        k["t-last_submission"] = ago(tot["last_submission"])
        tp = repo.throughput(c)
        k["tp-pages"] = num(tp["pages_per_min"])
        k["tp-caps"] = num(tp["captures_per_hour"])
        for d in repo.domain_overview(c):
            i = d["id"]
            for key in ("urls", "queued", "submitted", "success", "failed", "pending"):
                k[f"d{i}-{key}"] = num(d[key])
            k[f"d{i}-status"] = d["status"]
            st[f"d{i}-status"] = d["status"]
            k[f"d{i}-last_scan"] = ago(d["last_scan_at"])
            k[f"d{i}-last_submission"] = ago(d["last_submission_at"])
            scan = repo.running_scan(c, i)
            pr = scan_progress(c, scan)
            bar[f"d{i}"] = pr["pct"] if pr else 0
        html = {"log": _log_html(recent_events(c, limit=30))}
        token = db.scalar(c, "SELECT COUNT(*) FROM domains")
    else:
        ov = next((x for x in repo.domain_overview(c) if x["id"] == domain), None)
        if not ov:
            raise HTTPException(404)
        for key in ("urls", "queued", "submitted", "success", "failed", "blocked", "pending", "running"):
            k[f"o-{key}"] = num(ov[key])
        k["o-status"] = ov["status"]
        st["o-status"] = ov["status"]
        k["o-last_scan"] = ago(ov["last_scan_at"])
        k["o-last_submission"] = ago(ov["last_submission_at"])
        counts = repo.submission_counts(c, domain)
        for svc in repo.SERVICES:
            sc = counts.get(svc, {})
            for state in ("queued", "running", "retry", "success", "failed", "blocked"):
                k[f"s-{svc}-{state}"] = num(sc.get(state, 0))
        scan = repo.running_scan(c, domain)
        pr = scan_progress(c, scan)
        if pr:
            k["p-done"], k["p-total"] = num(pr["done"]), num(pr["total"])
            k["p-fetched"] = num(scan["pages_fetched"])
            k["p-rate"] = f"{pr['rate']:.0f}"
            k["p-elapsed"] = dur(pr["elapsed"])
            bar["scan"] = pr["pct"]
        total_subs = sum(v for kk, v in counts["_all"].items() if kk != "cancelled")
        finished = sum(counts["_all"].get(x, 0) for x in ("success", "failed", "blocked"))
        bar["archive"] = (100 * finished / total_subs) if total_subs else 0
        k["a-done"], k["a-total"] = num(finished), num(total_subs)
        html = {"log": _log_html(recent_events(c, domain, 25), show_host=False)}
        token = f"{scan['id'] if scan else 'idle'}"
    return JSONResponse({"k": k, "st": st, "bar": bar, "html": html, "token": str(token)})


def engine_line(e) -> str:
    if not e["workers"]:
        return "worker resting · wakes while this page is open" if e.get("serverless") else "worker offline"
    parts = [f"{e['workers']} worker{'s' if e['workers'] > 1 else ''}"]
    if e["paused"]:
        parts.append("queue paused")
    if e["scanning"]:
        parts.append(f"{e['scanning']} scanning")
    parts.append(f"{e['running']} in flight")
    return " · ".join(parts)


templates.env.globals["engine_line"] = engine_line


@app.get("/api/health")
def health():
    c = con()
    return {"ok": True, "version": config.VERSION, **engine_status(c)}
