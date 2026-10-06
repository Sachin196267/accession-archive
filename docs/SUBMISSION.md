# Submission details: Accession

Website Archive Submitter & Automated Backup Repository.

## Links

| Item | Link |
|---|---|
| Live deployment | https://accession-archive.vercel.app |
| Source code | https://github.com/Sachin196267/accession-archive |
| Video walkthrough (≈10 min, voice-over) | *(Google Drive / YouTube link, "anyone with the link")* |

## Project summary

Accession is a website archiving and backup tool. You add one or more domains. It
discovers each site's public URLs from links, sitemaps (including sitemap indexes and
gzip sitemaps), RSS/Atom feeds, robots.txt, canonical tags and pagination. It
normalizes and de-duplicates the URLs, then submits them through a persistent queue
to the Internet Archive's Wayback Machine, archive.today, and a local snapshot store.

Every URL, discovery source, submission, archive link, status and error is stored in a
searchable repository. Later scans detect new, missing and changed pages and archive
only what is new. The system resumes automatically after crashes or restarts.

## Technical details

| | |
|---|---|
| Language | Python 3.12+ |
| Web framework | FastAPI, server-rendered Jinja2 templates, small vanilla JavaScript for live updates (polling every 2 s) |
| HTTP client | httpx (async) |
| Database | SQLite locally (WAL mode); Turso (hosted libSQL, SQLite-compatible) in production, over its HTTP API |
| Background processing | Async worker engine: crawl loop, submission dispatcher with per-service rate limits, scheduler for recurring scans, heartbeat-based crash recovery. Runs in-process, as separate worker processes, or in bounded "bursts" on serverless hosting |
| Browser rendering (optional) | Playwright / headless Chromium for JavaScript-rendered pages (local install) |
| Hosting | Vercel (Python serverless function, `iad1` region), Turso database, daily Vercel cron |
| Tests | 20 automated tests (pytest). Unit tests plus end-to-end runs against a bundled demo website and mock Save Page Now / archive.today servers. Also run against a hosted-database stand-in |

**Architecture.** The web interface and the workers share one database and never call
each other directly. All state (crawl frontier, URL inventory, submission queue,
history) lives in the database. A crash, restart or extra worker never loses or
duplicates work:
- queue claims are atomic transactions;
- every claim carries the worker id and a heartbeat;
- stale work returns to the queue automatically;
- Save Page Now job ids are saved immediately, so an interrupted capture is re-polled
  instead of being submitted again.

**Main modules:**
- `crawler.py`: discovery
- `parse.py`: HTML, sitemap, feed and robots parsing
- `urls.py`: normalization
- `repo.py`: queue
- `worker.py`: engine
- `services/`: archive integrations
- `web/`: interface
- `remote.py`: hosted-database driver

Full details are in `docs/ARCHITECTURE.md`. A point-by-point mapping to the assignment
is in `docs/REQUIREMENTS.md`.

## Setup and run instructions

Requirements: Python 3.11 or newer, macOS / Linux / Windows.

```bash
git clone https://github.com/Sachin196267/accession-archive.git
cd accession-archive
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m accession serve          # open http://127.0.0.1:8000
```

Optional, for JavaScript rendering:

```bash
playwright install chromium
```

Offline demo website (sitemaps, feed, pagination, redirects, broken links):

```bash
python -m accession demo-site      # http://127.0.0.1:8765
```

Then add `http://127.0.0.1:8765/` in the interface with **Local snapshot** selected.
Open `http://127.0.0.1:8765/_demo/publish` and press **Scan again** to see new,
missing and changed URLs being detected.

Tests:

```bash
python -m pytest -q
```

Optional environment variables:

| Variable | Purpose |
|---|---|
| `IA_ACCESS_KEY`, `IA_SECRET_KEY` | archive.org keys for Wayback Machine captures (these can also be set on the System page) |
| `ACCESSION_HOME` | Data folder |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | Hosted database |
| `ACCESSION_PASSWORD` | Owner password |
| `ACCESSION_PUBLIC=1` | Public mode |

Deployment steps for Vercel are in `README.md`, section 4.

## External APIs and services used

| Service | What for |
|---|---|
| Internet Archive, Save Page Now 2 API (`web.archive.org/save`, `/save/status/<job>`) | Submitting captures to the Wayback Machine and polling their status. Authenticated with the archive.org S3 keys |
| Internet Archive Wayback Availability API (`archive.org/wayback/available`) | Optional: reuse a recent existing capture instead of making a new one |
| archive.today public submission form | Submitting to archive.today. CAPTCHA pages are detected and handed to the user, never bypassed |
| Turso (libSQL HTTP API) | Hosted database for the live deployment |
| Vercel | Hosting, serverless function and daily cron |
| Google Fonts | Interface typefaces (Instrument Serif, IBM Plex Sans / Mono) |

No paid APIs are used.

## AI usage

- **AI inside the application:** none. Accession does not call any AI or LLM API
  (no Gemini, Groq or OpenAI). Discovery, normalization and archiving are
  deterministic code.
- **AI used during development:** the project was built with the help of Claude Code
  (Anthropic's AI coding assistant), which was used for code, tests, documentation and
  the narrated demonstration video. The voice-over is macOS text-to-speech.

## Test login / demo credentials

- **No login is needed.** The live site is public: anyone can browse, search, add
  websites, scan, archive and export.
- **Owner-only actions** (deleting a domain, changing system settings, pausing
  services) need the owner password. It is not published, and can be shared with the
  reviewer on request.
- **Demo data:** example.com is already in the register. To try a full run, add any
  small public website (up to 500 pages for visitors) with **Local snapshot** selected.

## Known limitations

- The Wayback Machine now requires an archive.org login, so captures need the owner's
  archive.org keys. Without them, Wayback items wait in *Needs review* with a link to
  save them by hand.
- archive.today usually shows a CAPTCHA to automated clients. As the assignment
  requires, this is not bypassed: the item is marked *blocked*, and the user can
  submit it by hand and paste the archive link back.
- On Vercel, background work runs while a page of the site is open, plus one
  automatic daily run, because serverless functions cannot run continuously. Locally,
  a worker runs all the time.
- JavaScript rendering (Playwright) is available locally, not on Vercel.
- The Instagram bonus (login walls detected and reported, never bypassed) was not
  tested against Instagram itself.

## Results and evidence

All numbers come from real runs: the recorded walkthrough (database kept), the
automated test suite, and checks against the live deployment.

### 1. Discovery: bundled demo website, 0.55 s simulated server latency per request

| Scan | Pages fetched | URLs seen | New | No longer linked | Changed | Answered 304 | Errors | Time | Speed |
|---|---|---|---|---|---|---|---|---|---|
| Site A, scan 1 | 48 | 51 | 51 | 0 | 0 | 0 | 0 | 9.5 s | ≈ 304 pages/min |
| Site A, scan 2 (after the site was updated) | 54 | 57 | 7 | 1 | 6 | 39 | 0 | 11.1 s | ≈ 292 pages/min |
| Site B, scan 1 (second domain, same queue) | 48 | 50 | 50 | 0 | 0 | 0 | 0 | 10.6 s | ≈ 272 pages/min |

Without simulated latency, the first scan of site A took about 1 s (≈ 2,900 pages/min).

**Discovery sources** (rows in `url_sources`):

| Source | Rows |
|---|---|
| link | 88 |
| sitemap | 72 |
| feed | 24 |
| pagination | 7 |
| seed | 2 |
| redirect | 2 |
| canonical | 2 |
| external | 1 |

Three pages on each site were found only through the gzip sitemap. Nothing links to
them.

**Clean-up.** These were stored with the reason and not submitted:

| Reason | URLs |
|---|---|
| Disallowed by robots.txt | 2 |
| Canonical duplicates | 2 |
| Redirects to another URL in the inventory | 2 |
| External link, recorded only | 1 |
| HTTP 404 / 500 | 4 |

A link carrying `utm_*` tracking parameters was normalized to the clean URL and
stored once.

### 2. Incremental backup and change detection

- After the update (6 posts added, 1 page edited, 1 post removed), scan 2 queued
  **only the 7 new URLs**. The 45 pages already archived were not resubmitted.
- **39 of 54** fetches in scan 2 answered `304 Not Modified`, so they were not
  downloaded again. Their stored links were reused.
- Comparing the two snapshots of `/about` shows exactly the one edited sentence.

### 3. Archiving

| Submissions | Count |
|---|---|
| Local snapshots stored | 149 (97 KB of compressed copies) |
| Successful: new pages | 97 |
| Successful: explicit re-archive | 51 |
| Successful: manual request | 1 |
| Failed (genuine) | 1 |
| Average capture time | ≈ 0.56 s |

The one failure is `/blog/post-3`, which the site update had deleted, so it returned
404. It was recorded as failed and left visible for review.

### 4. Crash recovery

The server process was killed (`SIGKILL`) while re-archiving. After the restart, the
log recorded *"1 submission … recovered from an interrupted worker"*, and the queue
finished the remaining work. Nothing was lost and nothing was submitted twice. This is
shown on camera in chapter 9 of the video.

### 5. Automated tests

**20 / 20 passing.** The suite runs twice:
- against local SQLite;
- against a stand-in for the hosted database that is as strict as the real Turso
  server.

What the tests cover:
- every discovery source;
- de-duplication;
- incremental rescans with 304s;
- Save Page Now with and without keys (mock server);
- archive.today CAPTCHA, which is never bypassed;
- the Wayback login wall;
- permanent vs temporary failures and retries;
- crash recovery and resuming an interrupted scan;
- round-robin fairness and priority between domains;
- JavaScript rendering: three links that exist only after scripts run were found;
- serverless bursts.

### 6. Live deployment checks (https://accession-archive.vercel.app)

- Public pages load with no login. Owner-only actions (system settings, deleting a
  domain) redirect to sign-in.
- `example.com` was added, scanned and archived end to end on Vercel + Turso:
  1 URL found, 1 archived, 0 failed.
- Private and internal addresses (`localhost`, `127.0.0.1`) are refused, and visitor
  scans are capped at 500 pages.
- Testing on the real deployment found a production-only bug: Turso rejects unused
  query parameters, which SQLite silently ignores. It was fixed, and the test
  stand-in was made equally strict so the bug cannot return.

### Where to verify

| Evidence | Where |
|---|---|
| Video | Walkthrough, chapters 3, 8 and 9 |
| Live data | Register → domain page → **Scans** tab and **Activity** log |
| Tests | `python -m pytest -q` |
| Mapping to the assignment | `docs/REQUIREMENTS.md` |
