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
