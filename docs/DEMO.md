# Demonstration script

A run-through that covers every item in §19 *Mandatory demonstration*. Plan for about 8–10 minutes of recording.

## Before recording

```bash
source .venv/bin/activate
rm -rf data                                  # start from an empty repository
python -m accession demo-site                # terminal 1: http://127.0.0.1:8765
python -m accession serve                    # terminal 2: http://127.0.0.1:8000
```

Optional: put your archive.org keys in **System**, so Wayback submissions go through during the recording.
Pick one small real website you are allowed to archive, for example your own site or your college's.

---

### 1. Add one domain  *(§19.1)*

On the **Register** page, open *Add a domain*:
1. Enter `http://127.0.0.1:8765/`.
2. Tick **Local snapshot**. The public archives cannot reach localhost.
3. Set *External links* to **Record, don't archive**.
4. Keep *Start scanning now* ticked.
5. Click **Add to the register**.

### 2. Automatic discovery  *(§19.2)*

On the domain page, the progress line counts frontier items, pages and pages per minute live.
- Open the **Activity** tab and point out the sitemap index, then the plain and gzipped child sitemaps, being read.

### 3. URL inventory  *(§19.3)*

Open the **Inventory** tab, which shows the total count.
1. Filter **Found via → sitemap**. `archive-only-1..3` are linked from nowhere; only the sitemap knows them.
2. Filter **Found via → pagination**, then **feed**.
3. Filter **HTTP → 4xx** and then **5xx** to show the broken pages.
4. Point out the exclusions:
   - `/old-about`: redirects
   - `/products/?sort=price`: canonical duplicate
   - `/private/drafts`: robots.txt
   - `/about?utm_source=…`: stored as `/about`, with the tracking parameters gone
5. Open one URL to show the original vs normalized form and every discovery source.

### 4–6. Queue, submission, successes and failures, archive links  *(§19.4–19.7)*

1. The Archiving bar fills on its own, because new URLs were queued automatically.
2. Show the **Queue** page: per-service pace, *Working now*, *Up next*, throughput, slowest pages.
3. Back on the inventory, the Local snapshot column shows ✓ dates. Click one to open the stored copy.
4. **Real archives:** add your real domain with **Wayback Machine** (and **archive.today**) ticked and a page limit of about 20.
   - Watch the Wayback column fill with `web.archive.org/web/<timestamp>/…` links.
   - Open one in the browser.
5. **Failures:** open **Queue → Needs review**.
   - Wayback `error:not-found` for 404 pages.
   - archive.today usually shows **blocked** (CAPTCHA). Click *Submit by hand ↗*, complete it yourself, then paste the archive link back on the URL page. It is recorded as a success.

### 7. Interrupt and resume  *(§19.8)*

1. Start a scan on a larger site, or queue many submissions.
2. While it runs, press **Ctrl+C** in terminal 2. Or use `kill -9 <pid>` to show a crash.
3. Restart with `python -m accession serve`.
4. Point out:
   - The activity log line: "… recovered from an interrupted worker" (or "released" after Ctrl+C).
   - The scan continues from its frontier; it does not start over.
   - Submissions that were running are back in the queue. Wayback jobs re-poll their saved `job_id`.
   - Counters carry on from where they were.

### 8. Second scan: duplicates and new URLs  *(§19.9)*

1. Open <http://127.0.0.1:8765/_demo/publish>. This adds 6 posts, edits `/about` and removes post-3.
2. On the domain page, click **Scan again**.
3. In the **Scans** tab, scan #2 shows:
   - about 7 new URLs
   - 1 missing
   - about 6 changed
   - about 39 answered `304` (not downloaded again)
4. Filter the inventory **Scan → new in #2**. Only these were queued, and nothing archived before was sent again.
5. On `/about`, click **Request a snapshot**, then **Compare the latest two**. The diff shows the edited sentence.
6. **Search** for `http://127.0.0.1:8765/about`. The answer reads "Yes, archived 2 times".

### 9. Multi-domain processing  *(§19.10)*

1. Paste 3–4 domains at once in *Add a domain* (one per line).
2. The **Register** shows each with its own status, counts and last scan / submission.
3. On **Queue → By domain**, the domains take turns in the combined queue.
4. Mark a URL **important** on its page; it jumps to the front.
5. Optional: run `python -m accession worker` in a third terminal. **Queue → Workers** lists two live workers sharing the work.

### Closing shots

- **System:** settings, repository size, Playwright status.
- **Export:** `/export/all.csv`, the complete inventory with archive history.
- **Tests:** run `python -m pytest -q`. All 19 pass.
