"""SQLite repository.

The database is the single source of truth: discovered URLs, crawl frontier, the
submission queue and every archive attempt live here, so a crash or restart never
loses work. WAL mode lets the web server read while workers write, and `BEGIN
IMMEDIATE` transactions make queue claims atomic across several worker processes.
"""
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id                  INTEGER PRIMARY KEY,
    host                TEXT NOT NULL UNIQUE,
    root_url            TEXT NOT NULL,
    created_at          REAL NOT NULL,
    include_subdomains  INTEGER NOT NULL DEFAULT 0,
    external_mode       TEXT NOT NULL DEFAULT 'ignore',   -- ignore | record | archive
    max_pages           INTEGER NOT NULL DEFAULT 5000,
    max_depth           INTEGER NOT NULL DEFAULT 12,
    crawl_concurrency   INTEGER NOT NULL DEFAULT 4,
    render_js           INTEGER NOT NULL DEFAULT 0,
    respect_robots      INTEGER NOT NULL DEFAULT 1,
    services            TEXT NOT NULL DEFAULT 'wayback',
    auto_archive        INTEGER NOT NULL DEFAULT 1,
    rearchive_on_change INTEGER NOT NULL DEFAULT 0,
    rescan_hours        REAL,
    next_scan_at        REAL,
    priority            INTEGER NOT NULL DEFAULT 0,
    paused              INTEGER NOT NULL DEFAULT 0,
    last_scan_at        REAL,
    last_submission_at  REAL
);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY,
    domain_id     INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    number        INTEGER NOT NULL,
    state         TEXT NOT NULL DEFAULT 'running',       -- running | done | cancelled | failed
    trigger       TEXT NOT NULL DEFAULT 'manual',        -- manual | schedule | cli
    started_at    REAL NOT NULL,
    finished_at   REAL,
    worker        TEXT,
    lease_until   REAL,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    not_modified  INTEGER NOT NULL DEFAULT 0,
    urls_seen     INTEGER NOT NULL DEFAULT 0,
    urls_new      INTEGER NOT NULL DEFAULT 0,
    urls_missing  INTEGER NOT NULL DEFAULT 0,
    urls_changed  INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    capped        INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS scans_domain ON scans(domain_id, id);

CREATE TABLE IF NOT EXISTS urls (
    id             INTEGER PRIMARY KEY,
    domain_id      INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,          -- normalized form, the identity of the page
    original_url   TEXT NOT NULL,          -- exactly as first discovered
    source         TEXT NOT NULL,          -- first discovery method
    found_on       TEXT,
    first_seen_at  REAL NOT NULL,
    first_scan_id  INTEGER,
    last_seen_at   REAL,
    last_scan_id   INTEGER,
    is_external    INTEGER NOT NULL DEFAULT 0,
    skip_reason    TEXT,                   -- set when the URL must not be submitted
    priority       INTEGER NOT NULL DEFAULT 0,
    depth          INTEGER,
    sitemap_lastmod TEXT,
    http_status    INTEGER,                -- first response status (e.g. 301)
    final_status   INTEGER,                -- status after redirects
    final_url      TEXT,
    redirects      INTEGER NOT NULL DEFAULT 0,
    content_type   TEXT,
    content_length INTEGER,
    content_hash   TEXT,
    changed_at     REAL,
    title          TEXT,
    canonical_url  TEXT,
    etag           TEXT,
    last_modified  TEXT,
    outlinks       BLOB,                   -- zlib JSON of extracted links, reused on 304
    response_ms    INTEGER,
    fetched_at     REAL,
    fetch_error    TEXT,
    UNIQUE (domain_id, url)
);
CREATE INDEX IF NOT EXISTS urls_first_scan ON urls(domain_id, first_scan_id);
CREATE INDEX IF NOT EXISTS urls_last_scan ON urls(domain_id, last_scan_id);
CREATE INDEX IF NOT EXISTS urls_url ON urls(url);

CREATE TABLE IF NOT EXISTS url_sources (
    url_id   INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    source   TEXT NOT NULL,
    found_on TEXT,
    scan_id  INTEGER,
    seen_at  REAL NOT NULL,
    PRIMARY KEY (url_id, source)
);

CREATE TABLE IF NOT EXISTS frontier (
    id          INTEGER PRIMARY KEY,
    scan_id     INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    domain_id   INTEGER NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'page',           -- page | sitemap | feed
    depth       INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'pending',        -- pending | leased | done | skipped | failed
    worker      TEXT,
    lease_until REAL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    done_at     REAL,
    UNIQUE (scan_id, kind, url)
);
CREATE INDEX IF NOT EXISTS frontier_claim ON frontier(scan_id, state, kind, depth);
CREATE INDEX IF NOT EXISTS frontier_done ON frontier(done_at);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY,
    url_id          INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    domain_id       INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    service         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued',   -- queued | running | retry | success | failed | blocked | cancelled
    reason          TEXT NOT NULL DEFAULT 'new',      -- new | rearchive | changed | retry | manual
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    last_attempt_at REAL,
    next_attempt_at REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    worker          TEXT,
    lease_until     REAL,
    job_id          TEXT,
    archive_url     TEXT,
    archive_id      TEXT,
    http_status     INTEGER,
    error           TEXT,
    note            TEXT,
    manual_url      TEXT,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS sub_claim ON submissions(service, state, priority, id);
CREATE INDEX IF NOT EXISTS sub_url ON submissions(url_id, service, state);
CREATE INDEX IF NOT EXISTS sub_domain ON submissions(domain_id, state);
CREATE INDEX IF NOT EXISTS sub_finished ON submissions(finished_at);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    url_id        INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    submission_id INTEGER,
    captured_at   REAL NOT NULL,
    http_status   INTEGER,
    content_hash  TEXT NOT NULL,
    size          INTEGER NOT NULL,
    path          TEXT NOT NULL,
    rendered      INTEGER NOT NULL DEFAULT 0,
    title         TEXT,
    data          BLOB                     -- gzip body when snapshots live in the database
);
CREATE INDEX IF NOT EXISTS snapshots_url ON snapshots(url_id, id);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    domain_id INTEGER,
    level     TEXT NOT NULL DEFAULT 'info',
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_domain ON events(domain_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_state (
    service        TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 1,
    cooldown_until REAL,
    last_error     TEXT,
    last_ok_at     REAL
);

CREATE TABLE IF NOT EXISTS workers (
    id           TEXT PRIMARY KEY,
    host         TEXT,
    pid          INTEGER,
    role         TEXT,
    started_at   REAL,
    heartbeat_at REAL,
    stopped_at   REAL,
    status       TEXT
);
"""

_local = threading.local()


def plural(n, word, many=None) -> str:
    """plural(1, "URL") -> "1 URL", plural(3, "URL") -> "3 URLs"."""
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def now() -> float:
    return time.time()


def connect(path=None):
    """Local SQLite file, or a hosted libSQL/Turso database when DB_URL is set."""
    if config.DB_URL and path is None:
        from .remote import Connection

        con = Connection(config.DB_URL, config.DB_TOKEN)
        _ensure_schema(con)
        return con
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(con)
    return con


_schema_ready: set = set()


def _ensure_schema(con):
    """Create / upgrade the schema once per database per process."""
    key = config.DB_URL or str(config.DB_PATH)
    if key not in _schema_ready:
        _schema_ready.add(key)
        init_db(con)


def conn() -> sqlite3.Connection:
    """One connection per thread (sqlite3 connections are not thread-safe)."""
    c = getattr(_local, "con", None)
    key = config.DB_URL or config.DB_PATH
    if c is None or getattr(_local, "path", None) != key:
        c = connect()
        _local.con, _local.path = c, key
    return c


MIGRATIONS = [
    "ALTER TABLE snapshots ADD COLUMN data BLOB",
]


def init_db(con=None):
    con = con or conn()
    con.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass  # already applied
    for k, v in config.DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    for svc in ("wayback", "archive_today", "local"):
        con.execute("INSERT OR IGNORE INTO service_state(service) VALUES (?)", (svc,))
    return con


@contextmanager
def tx(con):
    """Write transaction that takes the write lock up front (no upgrade deadlocks)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def settings(con) -> dict:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def log(con, message, domain_id=None, level="info"):
    con.execute(
        "INSERT INTO events(ts, domain_id, level, message) VALUES (?, ?, ?, ?)",
        (now(), domain_id, level, message),
    )


def one(con, sql, *args):
    return con.execute(sql, args).fetchone()


def scalar(con, sql, *args):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None
