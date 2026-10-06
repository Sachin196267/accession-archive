# Accession

**Website Archive Submitter & Automated Backup Repository.**

Accession is a Python tool that takes one or many domains and works through them:

1. It discovers the public URLs of each domain.
2. It normalizes and de-duplicates them.
3. It submits them automatically to web-archiving services.
4. It keeps a permanent, searchable record of everything it found, sent and got back.

| | |
|---|---|
| Language | Python 3.11+ (tested on 3.14) |
| Web | FastAPI + server-rendered Jinja templates, a small vanilla-JS live updater |
| Storage | SQLite in WAL mode (one file) + gzip snapshot files |
| Archive services | Internet Archive Wayback Machine (Save Page Now 2), archive.today, local snapshot store |
| Optional | Playwright / headless Chromium for JavaScript-rendered pages |

---

## 1. Setup

```bash
cd "sma task"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # app + tests (requirements.txt is the app only)
```

Optional JavaScript rendering (bonus feature):

```bash
pip install playwright
playwright install chromium
```

### Run

```bash
python -m accession serve           # http://127.0.0.1:8000
```

`serve` starts the web interface and one background worker in the same process.
Data goes to `./data/` (database `accession.db` and `snapshots/`). To keep it somewhere
else, set `ACCESSION_HOME=/path/to/dir`.

### Internet Archive keys (recommended)

Save Page Now's API now answers anonymous clients with *"You need to be logged in to use Save Page Now"*.
Here is how to get keys:

1. Create a free archive.org account.
2. Open <https://archive.org/account/s3.php>.
3. Paste the two keys into **System → Internet Archive keys**, or export them as environment variables:

```bash
export IA_ACCESS_KEY=...
export IA_SECRET_KEY=...
```

Without keys the tool still tries the anonymous save endpoint. If the Archive asks for a
login, the URL goes to the **Needs review** list with a link to save it by hand. The
tool never tries to get around the login.

### Try it offline

A demo website ships with the project. It exercises every discovery path: robots.txt,
a sitemap index and a gzipped sitemap, an RSS feed, pagination, canonical duplicates,
redirects, 404/500 pages, tracking parameters and links created only by JavaScript.

```bash
python -m accession demo-site       # http://127.0.0.1:8765
```

Add `http://127.0.0.1:8765/` in the web interface with **Local snapshot** ticked.
The public archives cannot reach `localhost`, so use a real domain for Wayback and archive.today.
To simulate a website update, open <http://127.0.0.1:8765/_demo/publish>. It adds 6
posts, edits `/about` and removes one post. Then press **Scan again**.

### Tests

```bash
python -m pytest -q
```

19 end-to-end and unit tests. They run against the demo site and a mock SPN2 / archive.today server,
so no real archive is contacted. The tests cover:

- discovery from every source
- incremental rescans
- Save Page Now with and without keys
- CAPTCHA and login-wall handling
- permanent vs temporary failures
- crash recovery and resuming an interrupted scan
- round-robin fairness between domains
- JavaScript rendering

---

## 2. Using it

| Page | What it is for |
|---|---|
| **Register** (`/`) | Dashboard: domains, discovered / queued / submitted / archived / failed / pending counts, last scan and last submission, live crawl speed and capture throughput, service health, activity log. Add one or many domains here. |
| **Domain** (`/domains/<id>`) | One domain: status, live scan and archive progress, URL inventory with filters, per-service counts, submission history, scan history (new / missing / changed URLs), settings. |
| **URL** (`/urls/<id>`) | Everything about one URL: original vs normalized form, every discovery source, HTTP status and redirects, full archive history, local snapshots and diffs, "archive now" at high priority. |
| **Queue** (`/queue`) | The combined queue across all domains: working now, up next, retrying, **needs review** (failed / blocked), per-service pace and health, slowest pages and captures, workers. Pause / resume. |
| **Search** (`/search`) | Search by domain or URL. Paste a full URL to get a direct *yes / no* answer. Filter by service and submission state. |
| **System** (`/system`) | Archive keys, pacing, retries, parallel scans; repository size; Playwright status. |
| **Export** | `/export/all.csv`, `/export/all.json`, `/export/<domain-id>.csv` — the complete archival inventory. |

### Command line

```bash
python -m accession add example.com example.org --scan   # add domains and queue scans
python -m accession status                                 # print the register
python -m accession archive example.com                    # queue never-submitted URLs
python -m accession export inventory.csv                   # or .json
python -m accession worker                                 # an extra worker process
python -m accession serve --no-worker                      # web only; workers run separately
```

---

## 3. How the assignment requirements are covered

| Requirement | Where |
|---|---|
| Single domain: add, discover, list, count, submit, progress, store results, resume | Register → Domain page; `crawler.py`, `worker.py`, `repo.py` |
| Multiple domains, independent queues, combined queue, per-domain stats and history | `domains` table, per-domain `paused` / `priority`, round-robin in `repo.claim_submission` |
| Discovery: links, sitemap.xml, sitemap indexes, robots.txt, canonical, pagination, feeds, other navigation (`<area>`, meta refresh, hreflang), optional external-link modes | `crawler.py`, `parse.py` |
| URL processing: normalize, de-duplicate, fragments, redirects, HTTP status, failures, original URL kept | `urls.py`, `urls` table (`original_url`, `url`, `http_status`, `final_status`, `final_url`, `fetch_error`, `skip_reason`) |
| Wayback Machine + archive.today, without bypassing CAPTCHAs / logins / rate limits | `services/wayback.py`, `services/archive_today.py` |
| Persistent queue, sequential / controlled concurrency, start & finish times, success / failure, archive URL / ID, retry with backoff, continue after failures | `submissions` table, `repo.finish_submission`, `Engine._dispatch_loop` |
| Repository fields (domain, original / normalized URL, source, timestamps, service, status, archive URL / ID, HTTP status, error, last attempt) | `docs/ARCHITECTURE.md` §3 |
| Incremental backup, re-archive on request, multiple submissions per URL | `repo.enqueue_new` (never twice by accident), `repo.enqueue_rearchive` (explicit, adds history) |
| Website changes: rescan, new / missing / changed URLs without rebuilding | `scans` table, `first_scan_id` / `last_scan_id`, content hashes, ETag / Last-Modified |
| Dashboard, search, filters, history | Register, Domain, Queue, Search pages |
| Large sites: database not memory, background workers, batches, no duplicate crawling, no needless re-downloads, crash / restart resume | `frontier` table with `UNIQUE(scan_id, kind, url)`, leases + heartbeats, 304 reuse of stored links |
| JavaScript-rendered sites (investigation + bonus) | `render.py`, "Render JavaScript" option, `js-render` source, rendered local snapshots |
| Performance: crawl speed, throughput, slow services / URLs, background processing, concurrency, retry & backoff, live progress | Queue page, scan table, `/api/live` polling every 2 s |
| Failure recovery | `repo.recover`, worker heartbeats, *Needs review* list |

### Bonus items

- **More than two archive services:** Wayback, archive.today and the local snapshot store.
- **50+ domains at once:** the combined queue serves domains round-robin, and several scans run in parallel (configurable).
- **Very large sites:** the frontier and inventory live in SQLite, and memory use doesn't grow with site size.
- **JavaScript rendering:** Playwright / Chromium, used for discovery and for snapshots.
- **Instagram and other JS-heavy pages:** rendering plus login-wall detection; see the architecture doc.
- **Scheduled rescans** and **automatic detection of new URLs**.
- **Change detection before re-archiving:** content hashes, with an optional "re-archive pages that changed" setting.
- **Snapshot comparison:** a visible-text or HTML diff between two local snapshots.
- **Distributed workers:** `python -m accession worker`, run as many as you like.
- **Priority queue for important URLs.**
- **Sitemap discovery and refresh** on every scan.
- **CSV / JSON export.**

---

## 4. Live deployment on Vercel

Vercel runs code only for the length of a request. It has no always-on process and
no lasting disk. Accession adapts like this when it detects Vercel:

| Locally | On Vercel |
|---|---|
| SQLite file in `data/` | Hosted libSQL database on [Turso](https://turso.tech) (same SQL), via `accession/remote.py` |
| Background worker thread, always on | **Bursts** of up to 40 s via `/api/tick`. Any open page keeps one burst running at a time. A daily Vercel cron run handles scheduled rescans. |
| Snapshots as `.gz` files | Snapshots stored in the database |
| Open to whoever runs it | Protected by `ACCESSION_PASSWORD` |

Scans and archiving move forward while someone has the site open. Close every tab,
and work pauses until the next visit or the daily cron. Nothing is lost: the queue
lives in the database, and an interrupted burst resumes exactly like an interrupted
worker. JavaScript rendering is off on Vercel, because there is no Chromium there.

### One-time setup

1. **Database.** At <https://app.turso.tech>:
   - Create a database in **AWS US East (Virginia)**, next to Vercel's `iad1` region.
   - Copy its URL (`libsql://….turso.io`).
   - Create an auth token.
2. **Vercel login:**

```bash
npx vercel login
```

3. **Project and environment variables.** Run from the project folder. Each `env add`
   asks for the value:

```bash
npx vercel link --yes --project accession-archive
npx vercel env add TURSO_DATABASE_URL production
npx vercel env add TURSO_AUTH_TOKEN production
npx vercel env add ACCESSION_PASSWORD production   # the password for the site
npx vercel env add CRON_SECRET production          # any long random string, e.g. `openssl rand -hex 32`
npx vercel env add ACCESSION_PUBLIC production     # optional: 1 = anyone may use the site; the password then only guards owner actions
npx vercel env add IA_ACCESS_KEY production        # optional, archive.org keys
npx vercel env add IA_SECRET_KEY production        # optional
```

4. **Deploy:**

```bash
npx vercel deploy --prod
```

### Rehearse the Vercel setup locally

```bash
ACCESSION_SERVERLESS=1 TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... ACCESSION_PASSWORD=... \
  uvicorn api.index:app --port 8001
```

To run the test suite against a hosted-database stand-in:

```bash
ACCESSION_TEST_DB=remote python -m pytest -q
```

## 5. Responsible use

- robots.txt is respected by default, including `Crawl-delay`.
- Every request identifies itself with an `Accession/1.0` user agent.
- Each archive service has its own pace, set below its published limits. A rate-limit
  answer pauses that service instead of retrying harder.
- The tool does not solve CAPTCHAs, sign in to sites or bypass access controls.
  Pages behind those end up in the review list for a person to handle.

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/DEMO.md`](docs/DEMO.md).
