# Assignment checklist

Every point of *Website Archive Submitter & Automated Backup Repository*, and where
to see it in Accession. ✅ = done. ◐ = done with a limit, explained in the row.

## 3. Single domain support

| Requirement | Status | Where |
|---|---|---|
| Enter domain / website URL | ✅ | Register → *Add a domain* |
| Start discovery | ✅ | *Start scanning now*, or **Scan again** on the domain page |
| Display discovered URLs | ✅ | Domain → **Inventory** tab, with filters |
| Show total URLs found | ✅ | *URLs found* on the Register and *URLs* on the domain page |
| Start archival submission | ✅ | **Archive new URLs** (or automatic after each scan) |
| Show progress | ✅ | Discovery and Archiving bars on the domain page, live every 2 s |
| Store successful and failed submissions | ✅ | `submissions` table; **Submission history** tab |
| Resume after interruption | ✅ | Leases and heartbeats; tested by `test_crash_recovery_*` and `test_interrupted_scan_resumes` |

## 4. Multi-domain support

| Requirement | Status | Where |
|---|---|---|
| Multiple domain projects | ✅ | Add several domains at once (one per line) |
| Domain-level status | ✅ | Status stamp per domain: scanning / archiving / paused / idle |
| Independent URL queues | ✅ | Each domain can be paused or prioritised separately; Queue → *By domain* |
| Combined processing queue | ✅ | Queue page: one shared queue, domains served in turn |
| Per-domain statistics | ✅ | Register table and the domain page counters |
| Per-domain archive history | ✅ | Domain → **Submission history** and **Scans** |

## 5. URL discovery

| Requirement | Status | Where |
|---|---|---|
| Internal HTML links | ✅ | `crawler.py` / `parse.py`. Source shown as *link* |
| sitemap.xml | ✅ | Source *sitemap* |
| Sitemap indexes | ✅ | Followed recursively, gzip included |
| robots.txt references | ✅ | `Sitemap:` lines read; `Disallow` respected; `Crawl-delay` honoured |
| Canonical URLs | ✅ | Source *canonical*; duplicates excluded |
| Pagination | ✅ | `rel=next/prev`, `?page=N`, `/page/N/`. Source *pagination* |
| Feed URLs | ✅ | RSS/Atom. Source *feed* |
| Other navigation structures | ✅ | `<area>`, meta refresh, hreflang alternates, redirects |
| Stay in domain unless external mode | ✅ | *External links*: ignore / record / record and archive |

## 6. URL processing

| Requirement | Status | Where |
|---|---|---|
| Normalize URLs | ✅ | `urls.normalize` (case, ports, dot segments, encoding, sorted query) |
| Remove duplicates | ✅ | Unique normalized URL per domain; tracking parameters dropped; canonical duplicates excluded |
| Handle fragments | ✅ | `#section` dropped; `#!` hash-bang routes kept |
| Identify redirects | ✅ | First and final status, final URL, redirect count on each URL |
| Record HTTP status | ✅ | *HTTP* column; filter 2xx / 3xx / 4xx / 5xx |
| Identify inaccessible URLs | ✅ | Fetch errors and 4xx/5xx recorded and not submitted |
| Keep the original discovered URL | ✅ | `original_url` (*As found* on the URL page) |

## 7. Archive services

| Requirement | Status | Where |
|---|---|---|
| Internet Archive / Wayback Machine | ◐ | `services/wayback.py`, Save Page Now 2 API. The Archive now requires an archive.org login, so captures need the owner's free S3 keys (System page). Without keys, items wait in *Needs review* with a manual link. |
| Archive.today | ◐ | `services/archive_today.py`, public form. It usually answers automated clients with a CAPTCHA, which is never bypassed: items go to *Needs review* with a manual link, and the resulting archive link can be pasted back. |
| No bypassing of CAPTCHAs, logins or rate limits | ✅ | Detected; the service pauses itself; manual review |

## 8. Automated submission queue

| Requirement | Status | Where |
|---|---|---|
| Persistent queue | ✅ | `submissions` table |
| Sequential / controlled concurrency | ✅ | Per-service concurrency and spacing (System page) |
| Record start and completion times | ✅ | `started_at`, `finished_at`, `last_attempt_at` |
| Record success / failure | ✅ | States success / failed / blocked / retry |
| Store archive URLs / identifiers | ✅ | `archive_url`, `archive_id`, SPN2 `job_id` |
| Retry temporary failures | ✅ | Exponential backoff with jitter, up to *max attempts* |
| Continue after failures | ✅ | One URL failing never stops a scan or the queue |

## 9. Repository fields

All are stored, and all appear in **Export CSV / JSON**:

| Field | Column |
|---|---|
| domain | `domains.host` |
| original URL | `urls.original_url` |
| normalized URL | `urls.url` |
| discovery source | `urls.source`, `url_sources` |
| discovery timestamp | `urls.first_seen_at` |
| submission service | `submissions.service` |
| submission timestamp | `submissions.started_at` |
| submission status | `submissions.state` |
| archive URL | `submissions.archive_url` |
| archive identifier | `submissions.archive_id` |
| HTTP status | `urls.http_status`, `urls.final_status` |
| error message | `submissions.error`, `urls.fetch_error` |
| last attempted time | `submissions.last_attempt_at` |

## 10–11. Incremental backup and website changes

| Requirement | Status | Where |
|---|---|---|
| Remember discovered URLs | ✅ | The inventory persists across scans |
| Identify newly discovered URLs | ✅ | *New* per scan; inventory filter *Scan → new in #N* |
| URLs never archived by the tool | ✅ | Filter *Archive state → never archived*; *never archived* count |
| Re-archive on request | ✅ | **Re-archive** (all or changed pages) and *Request a snapshot* per URL |
| History of several submissions per URL | ✅ | URL page → *Archive history* |
| Rescan later, find new pages | ✅ | **Scan again**: new / missing / changed counts; only new URLs are queued; unchanged pages answer 304 and are not downloaded again |

## 12. Dashboard

Register page:
- domains, total URLs discovered, queued, submitted, successful, failed, pending
- **archive service used** (with captures per service) and **latest archive links**
- last scan and last submission time
- current processing status per domain and for the worker

## 13. Search and repository

| Requirement | Status | Where |
|---|---|---|
| Search by domain or URL | ✅ | Search page. A full URL gets a direct yes/no answer |
| Filter by archive service | ✅ | *Service* filter |
| Filter by submission status | ✅ | *Submission* filter |
| Open stored archive URL | ✅ | ✓ marks link to the capture |
| View submission history | ✅ | URL page |

## 14, 17, 18. Large sites, performance, failure recovery

| Requirement | Status | Where |
|---|---|---|
| Database instead of memory | ✅ | Frontier and inventory are tables |
| Queues and background workers | ✅ | Engine plus `python -m accession worker` |
| Batches | ✅ | Per-page transactions; queueing done in single SQL statements |
| No duplicate crawling | ✅ | `UNIQUE (scan, kind, url)` |
| No needless re-downloads | ✅ | ETag / Last-Modified; links reused on 304 |
| Resume after crash or restart | ✅ | Leases, heartbeats, saved SPN2 job ids |
| Crawl speed and submission throughput | ✅ | Pages/min, captures/hour, per-scan speed |
| Slow services and URLs | ✅ | Queue → *Slowest pages / captures*, average per service |
| Retry and backoff | ✅ | See section 8 |
| Real-time progress | ✅ | Live updates every 2 s |
| Server restart loses nothing; failures visible | ✅ | *Needs review* list |

## 15–16. JavaScript-rendered sites and Instagram (bonus)

| Requirement | Status | Where |
|---|---|---|
| Investigation of JS rendering | ✅ | `docs/ARCHITECTURE.md` → *JavaScript-rendered sites* |
| Playwright rendering for discovery and snapshots | ✅ | Tested locally |
| Rendering on the live Vercel site | ◐ | Not available there (no browser in serverless functions) |
| Instagram | ◐ | Login walls are detected and reported, never bypassed; not run against Instagram itself |

## 19. Mandatory demonstration

The step-by-step script is in [DEMO.md](DEMO.md). Each item is covered:
- add a domain
- discover and show the inventory
- the queue
- submission
- successes and failures
- archive links
- interrupt and resume
- second scan
- multi-domain

## 20. Deliverables

| Deliverable | Status | Where |
|---|---|---|
| Source code | ✅ | GitHub `Sachin196267/accession-archive` |
| Web interface | ✅ | Live at https://accession-archive.vercel.app |
| Crawler | ✅ | `crawler.py`, `parse.py` |
| Archive integrations | ✅ | `services/` |
| Database / schema | ✅ | `db.py`, documented in ARCHITECTURE.md |
| Queue | ✅ | `repo.py`, `worker.py` |
| Dashboard | ✅ | Register page |
| Architecture documentation | ✅ | `docs/ARCHITECTURE.md` |
| Setup instructions | ✅ | `README.md` |
| Demonstration video | ⬜ | To record, following DEMO.md |

## 21. Bonus challenges

- More than two services ✅
- 50+ domains ✅
- Very large sites ✅ (design; the live site caps visitors at 500 pages)
- JavaScript capture ✅ (locally)
- Instagram ◐
- Scheduled rescans ✅
- Automatic new-URL detection ✅
- Change detection ✅
- Snapshot comparison ✅
- Distributed workers ✅
- Priority queue ✅
- Sitemap refresh ✅
- CSV/JSON export ✅
