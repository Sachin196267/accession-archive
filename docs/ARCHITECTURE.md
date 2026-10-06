# Accession — technical architecture

## 1. Overview

```
                    ┌────────────────────────────────────────────┐
  browser  ───────► │  FastAPI web app (accession/web/app.py)     │
  (pages +          │  server-rendered pages, /api/live JSON,     │
   2 s polling)     │  CSV/JSON export, snapshot viewer & diff    │
                    └───────────────┬────────────────────────────┘
                                    │ reads / writes rows
                                    ▼
                    ┌────────────────────────────────────────────┐
                    │  SQLite (WAL) — the single source of truth  │
                    │  domains · scans · urls · url_sources ·     │
                    │  frontier · submissions · snapshots ·       │
                    │  events · settings · service_state · workers│
                    └───────────────▲────────────────────────────┘
                                    │ atomic claims (BEGIN IMMEDIATE)
              ┌─────────────────────┴───────────────────────┐
              │  Engine / worker (accession/worker.py)       │  × N processes
              │  ├─ scan loop      → crawler.Scan (async)    │
              │  ├─ dispatch loop  → services.* (async)      │
              │  ├─ schedule loop  → recurring rescans       │
              │  └─ heartbeat loop → liveness + recovery     │
              └──────────────────────────────────────────────┘
                     │ httpx (async)            │ Playwright (optional)
                     ▼                          ▼
          target websites            Wayback SPN2 · archive.today · local store
```

The web process and the workers never talk to each other directly. All state lives in
the database:

- Pressing **Scan** inserts a `scans` row.
- Pressing **Archive** inserts `submissions` rows.
- Workers pick that work up.

So a restart of any process loses nothing, and extra workers can be added at any time.

## 2. Modules

| Module | Responsibility |
|---|---|
| `config.py` | Data location, user agent, default settings |
| `db.py` | Schema, connections (one per thread, WAL, busy timeout), transactions, settings, event log |
| `urls.py` | Normalization, scope rules, asset / pagination detection |
| `parse.py` | Standard-library parsers for HTML links, sitemaps (index / urlset / gzip / text), RSS / Atom, robots.txt |
| `fetch.py` | Async HTTP with size cap, timing, redirect chain, conditional requests |
| `render.py` | Optional headless Chromium; login-wall detection |
| `crawler.py` | One scan: seeding, frontier claims, page / sitemap / feed handlers, finish statistics |
| `repo.py` | Queue operations shared by web, CLI and workers: enqueue, claim, finish, retry, recover, stats |
| `services/` | `wayback.py`, `archive_today.py`, `local.py` behind one `ArchiveService` interface |
| `worker.py` | The engine: loops, pacing per service, cooldowns, graceful shutdown |
| `web/` | Routes, templates, CSS, live-update script |
| `demo_site.py` | Deterministic test website, with a "publish" switch for change detection |

## 3. Database schema (main tables)

**`domains`**: one row per website project.
- Crawl options: `max_pages`, `max_depth`, `crawl_concurrency`, `include_subdomains`, `external_mode`, `render_js`, `respect_robots`.
- Archive options: `services`, `auto_archive`, `rearchive_on_change`.
- Scheduling and control: `rescan_hours`, `next_scan_at`, `priority`, `paused`, `last_scan_at`, `last_submission_at`.

**`scans`**: one row per discovery run.
- `number`, `state`, `trigger`, `started_at`, `finished_at`.
- Counters: `pages_fetched`, `not_modified` (304s), `urls_seen`, `urls_new`, `urls_missing`, `urls_changed`, `errors`, `bytes`, `capped`.

**`urls`**: the inventory, `UNIQUE(domain_id, url)`.

| Field | Meaning |
|---|---|
| `url` / `original_url` | normalized identity / exactly as first discovered |
| `source`, `found_on` | first discovery method and the page it was found on |
| `first_seen_at`, `first_scan_id`, `last_seen_at`, `last_scan_id` | discovery timestamps; new / missing detection |
| `http_status`, `final_status`, `final_url`, `redirects` | first response, response after redirects |
| `content_type`, `content_length`, `content_hash`, `changed_at` | change detection |
| `etag`, `last_modified`, `outlinks` | avoid re-downloading: conditional GET, links reused on 304 |
| `canonical_url`, `title`, `depth`, `sitemap_lastmod` | page metadata |
| `skip_reason` | why it is not auto-submitted (robots, redirect, canonical duplicate, external) |
| `fetch_error`, `response_ms`, `fetched_at` | failures and timing |

**`url_sources`**: every way a URL was found. One row per (url, source):
`seed`, `link`, `sitemap`, `feed`, `pagination`, `canonical`, `redirect`, `alternate`, `meta-refresh`, `js-render`, `external`.

**`frontier`**: the crawl to-do list of a scan, `UNIQUE(scan_id, kind, url)`, so a page is never fetched twice in one scan.
- `kind`: page / sitemap / feed.
- `state`: pending → leased → done / skipped / failed.
- Also `worker`, `lease_until`, `attempts`.

**`submissions`**: the queue *and* the archive history. Every attempt to archive a URL at a service is one row; re-archiving adds a row.

| Field | Meaning |
|---|---|
| `service`, `state` | `queued`, `running`, `retry`, `success`, `failed`, `blocked`, `cancelled` |
| `reason` | `new`, `rearchive`, `changed`, `manual`, `retry` |
| `priority` | higher is claimed first |
| `created_at`, `started_at`, `finished_at`, `last_attempt_at`, `next_attempt_at`, `attempts` | timing and retry schedule |
| `job_id` | SPN2 job, stored the moment it is issued |
| `archive_url`, `archive_id`, `http_status`, `error`, `note`, `manual_url`, `duration_ms` | result |
| `worker`, `lease_until` | ownership while running |

**Other tables:**
- `snapshots`: local copies (gzip path, sha256, size, rendered flag).
- `events`: activity log.
- `service_state`: enabled flag, cooldown and last error per service.
- `workers`: heartbeats.
- `settings`: key / value pairs.

## 4. Discovery

A scan is a breadth-first walk over a database frontier, seeded with:

1. **robots.txt**: `Sitemap:` lines, `Disallow` rules (respected by default) and `Crawl-delay` (honoured, capped at 30 s).
2. **Sitemaps**: those listed in robots.txt, or the usual guesses (`/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`).
   - Index files are followed recursively.
   - Gzip and plain-text sitemaps are supported.
   - `<lastmod>` is stored.
3. **Feeds**: `<link rel=alternate type=rss/atom>` on any page, plus a few guesses (`/feed`, `/rss.xml`, …). Item links are added.
4. **The start page.**

Each HTML page contributes:
- `<a>` and `<area>` links
- `rel=canonical`
- `rel=next/prev` and `?page=N` / `/page/N` pagination
- hreflang alternates
- feed links
- meta-refresh targets

With JavaScript rendering on, the page is also opened in Chromium. Links that exist only in
the rendered DOM are recorded with source `js-render`, and the scan notes how many there were.

**Scope.** `www.` and the bare host count as the same site. Subdomains are included only when asked.
External links are handled by one of three modes:
- *ignore*
- *record* (inventory only)
- *archive* (inventory and submitted, never crawled)

**Normalization.**
- Lower-case scheme and host; IDNA hosts.
- Default ports dropped.
- Dot-segments resolved.
- Percent-escapes upper-cased, with unreserved characters decoded.
- Tracking parameters removed (`utm_*`, `fbclid`, `gclid`, …) and the remaining query sorted.
- Fragments removed, except AJAX hash-bangs (`#!`), which address real pages.
- The URL exactly as discovered is kept in `original_url`.

**De-duplication and exclusion.** Several kinds of URL are kept in the inventory but not auto-submitted, each with a `skip_reason`:
- a URL that redirects to another URL in the inventory
- a URL whose canonical points elsewhere
- a URL disallowed by robots.txt
- a URL that answers with 4xx / 5xx or a network error

Static assets (CSS, JS, images, fonts, media) are not treated as pages. The archive services capture them along with the page.

**Large sites.**
- Nothing is held in memory beyond one page at a time.
- The frontier and inventory are indexed tables.
- The page limit stops fetching but keeps every discovered URL. Sitemap-listed pages can still be submitted.
- While a long crawl runs, the archive queue is fed every 20 s, so archiving starts before discovery ends.

## 5. Submission queue and services

**Claiming.** `repo.claim_submission` runs in a `BEGIN IMMEDIATE` transaction:
- It takes the highest-priority eligible row.
- Otherwise it rotates through domains (round-robin), so one large site cannot starve the others.
- Eligible means: state `queued`, or state `retry` whose `next_attempt_at` has passed, and the domain is not paused.

**Pacing.** Each service has a concurrency and a minimum interval between starts. The defaults sit under the published limits:

| Service | Default pace |
|---|---|
| Wayback, with keys | 1 capture / 9 s (SPN2 allows 7 / min) |
| Wayback, anonymous | 1 / 21 s (3 / min) |
| archive.today | 1 / 30 s |
| Local snapshot | 4 in parallel, no delay |

**Outcomes.**
- `success`: stores `archive_url` / `archive_id`.
- `retry`: a temporary error. Exponential backoff `30 s × 2^(attempt-1)` (±15 % jitter, capped at 6 h). After `max_attempts` the row becomes `failed`.
- `failed`: a permanent error (e.g. SPN2 `error:not-found`, `error:blocked-url`).
- `blocked`: a human is needed (CAPTCHA or login). Carries a `manual_url`. The person can submit by hand and paste the archive link back.

Rate-limit answers set a **cooldown** on the whole service instead of hammering it.
Failed and blocked rows never re-enter the queue on their own. They wait in *Needs review*.

### Wayback Machine (Save Page Now 2)

What was investigated (October 2026):
- The documented API is `POST /save` (form `url=`, `skip_first_archive=1`) with `Authorization: LOW access:secret`, then polling `GET /save/status/<job_id>` until `status` is `success` / `error`.
- Anonymous calls to the status endpoint currently answer `{"message": "You need to be logged in to use Save Page Now."}`.

The resulting design:
- **With keys:** SPN2. The `job_id` is saved before polling starts, so a worker that dies mid-capture is replaced by one that keeps polling the same job instead of capturing twice.
- **Without keys:** `GET /save/<url>`. A `/web/<14-digit timestamp>/` redirect means success. A login demand makes the row `blocked` and pauses the service for an hour.
- **`status_ext` errors** are classified:
  - permanent: not-found, no-access, blocked-url, invalid syntax, …
  - temporary: 5xx, timeouts, job-failed
  - cooldown: too-many-requests, session limit, bandwidth
- **Optional "reuse capture younger than N days":** queries the availability API first and records the existing snapshot instead of making a new one.

### archive.today

There is no official API. The public form works like this:
- `GET /` returns a hidden `submitid`.
- `POST /submit/` then redirects (`Location` or `Refresh` header) to `/<id>`, or to `/wip/<id>` while the capture is still processing.

Automated clients usually get HTTP 429 with a CAPTCHA page. When that happens:
1. The row becomes `blocked` with a `?run=1&url=` link for manual submission.
2. The service cools down for 30 minutes.

No attempt is made to solve or avoid the CAPTCHA.

### Local snapshot store

- Downloads the page, or the rendered DOM when rendering is on.
- Stores it gzip-compressed under `data/snapshots/<domain>/<url>/<time>-<hash>.html.gz`.
- Indexes it by sha256.

This gives the repository its own copy, change detection and snapshot comparison
(`/compare?a=&b=`, a visible-text or HTML diff). Snapshots are served with a
`Content-Security-Policy: sandbox; script-src 'none'` header, so archived pages cannot run scripts.

## 6. Incremental backups and website changes

- `enqueue_new` only queues a (URL, service) pair that has **no** earlier non-cancelled submission. Running "Archive new URLs" again never re-sends anything.
- `enqueue_rearchive` is the explicit "updated snapshot" path. It adds a new history row, either for every URL or only for URLs whose content changed in the last scan.
- Each scan stamps the URLs it sees with `last_scan_id`:
  - **new** URLs are those with `first_scan_id = this scan`
  - **missing** URLs are those not seen this time
  - **changed** URLs are those whose body hash differs
- Pages that send `ETag` / `Last-Modified` are re-requested conditionally. On `304 Not Modified` the links stored from the last visit are reused, so nothing is downloaded again.

**Example.** The demo site's second scan finds 7 new URLs, 1 missing and 6 changed.
39 of its 54 pages answer 304. Only the 7 new URLs are queued.

## 7. Failure recovery

Every worker writes a heartbeat every 2 s. Every claimed frontier row and submission carries the worker id and a lease.

| Event | What happens |
|---|---|
| Graceful stop (Ctrl+C / SIGTERM) | The worker hands back everything it held (attempt not counted) and marks itself stopped |
| Crash / `kill -9` / power loss | Within 20 s another (or the restarted) worker sees the stale heartbeat. The held rows return to the queue; a running scan continues from its frontier. |
| One URL fails | Only that row is affected. Crawl exceptions are logged and the scan moves on. |
| Repeated failure | The row stays `failed` with its reason, visible in *Needs review* |
| Wayback job in progress during a crash | The stored `job_id` is polled again rather than resubmitted |

## 8. JavaScript-rendered sites (investigation)

**Effect on discovery.** Single-page apps often ship an almost empty HTML shell, with navigation built by script.
A plain HTTP crawler sees none of those links. On the demo site, `/app/` builds three links in the browser.
They are found only with **Render JavaScript** on, and the scan records "3 links only visible after JavaScript rendering".

**Effect on archival quality.**
- Wayback's SPN2 runs a headless browser itself (`js_behavior_timeout`), so its captures already contain the rendered page.
- archive.today also renders.
- For the local store, rendering means the stored copy is the DOM people actually see, not the shell.

**Cost.** Rendering is about 10–50× slower than a plain fetch. That is why it is a per-domain switch, and why it is limited to 2 parallel pages.

**Instagram and other gated platforms.**
- The renderer loads the page as an anonymous visitor.
- If the platform redirects to a login, challenge or sign-in URL, the capture is recorded as failed with "page redirects to a login screen; not captured".
- Nothing signs in, reuses cookies or gets around the wall.
- Public pages that do render are captured like any other.

**Limit of this investigation.** Behaviour against Instagram itself was not tested during development. It changes often, and live testing was out of scope. The login-wall detection is a pattern on the final URL (`/accounts/login`, `/challenge`, …).

## 9. Scaling notes

- One SQLite file comfortably holds millions of URL rows. Every hot query is indexed (claims, inventory filters, per-scan counts). WAL lets the web UI read while workers write.
- Throughput is bounded by the archive services' own limits, not by the tool. For example, 7 captures / min per Internet Archive account comes to about 10 000 per day.
- Several worker processes can share one database on one machine. The `crawl` and `archive` roles can be split.
- For true multi-host distribution, the same claim / lease design maps directly onto PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED`).
