"""Record a narrated walkthrough video of Accession (MP4).

    python tools/record_video.py [output.mp4]

What it does:
  1. starts a fresh local copy of the app plus two demo websites,
  2. turns every narration line into speech with macOS `say`,
  3. drives a real Chromium window through the features with Playwright while
     recording it (captions and a visible cursor are drawn into the page),
  4. lays each spoken line on the timeline at the moment its step started and
     muxes video + voice into an H.264/AAC MP4 with ffmpeg.

Environment: VOICE (default Samantha), RATE (words per minute, default 172),
LIVE_URL (shown in the intro; empty to skip).
"""
import asyncio
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "accession-walkthrough.mp4"
WORK = Path(os.environ.get("VIDEO_WORK", "/tmp/accession-video")).resolve()
VOICE = os.environ.get("VOICE", "Samantha")
RATE = os.environ.get("RATE", "150")
LIVE_URL = os.environ.get("LIVE_URL", "https://accession-archive.vercel.app/")
APP = "http://127.0.0.1:8010"
DEMO = "http://127.0.0.1:8765/"
DEMO2 = "http://127.0.0.1:8766/"
W, H = 1600, 900
DB = WORK / "home" / "accession.db"

# ----------------------------------------------------------------------------- page overlay

OVERLAY_JS = r"""
(() => {
  const css = `
    #__cur{position:fixed;width:20px;height:20px;border-radius:50%;border:2px solid #b0361c;
      background:rgba(176,54,28,.18);transform:translate(-50%,-50%);z-index:2147483647;pointer-events:none;left:-40px;top:-40px}
    #__cap{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);max-width:1050px;width:max-content;
      background:rgba(29,28,25,.92);color:#f4f1ea;font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;
      padding:10px 18px;z-index:2147483646;pointer-events:none;border-left:3px solid #b0361c}
    #__cap:empty{display:none}
    #__chap{position:fixed;left:24px;top:18px;z-index:2147483646;pointer-events:none;background:#1d1c19;color:#f4f1ea;
      font:500 12px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;padding:8px 12px}
    #__chap:empty{display:none}`;
  function boot() {
    if (document.getElementById('__cur')) return;
    const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    for (const id of ['__cur','__cap','__chap']) { const d = document.createElement('div'); d.id = id; document.body.appendChild(d); }
    const cur = document.getElementById('__cur');
    try {
      const p = JSON.parse(sessionStorage.getItem('__pos') || 'null');
      if (p) { cur.style.left = p[0] + 'px'; cur.style.top = p[1] + 'px'; }
      document.getElementById('__cap').textContent = sessionStorage.getItem('__capt') || '';
      document.getElementById('__chap').textContent = sessionStorage.getItem('__chapt') || '';
    } catch (e) {}
    document.addEventListener('mousemove', e => {
      cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px';
      try { sessionStorage.setItem('__pos', JSON.stringify([e.clientX, e.clientY])); } catch (x) {}
    }, true);
    document.addEventListener('mousedown', () => cur.animate(
      [{transform:'translate(-50%,-50%) scale(1)'},{transform:'translate(-50%,-50%) scale(1.9)'},{transform:'translate(-50%,-50%) scale(1)'}],
      {duration: 380}), true);
  }
  window.__say = (cap, chap) => {
    try { sessionStorage.setItem('__capt', cap); if (chap !== undefined) sessionStorage.setItem('__chapt', chap); } catch (e) {}
    boot();
    document.getElementById('__cap').textContent = cap;
    if (chap !== undefined) document.getElementById('__chap').textContent = chap;
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
"""

# ----------------------------------------------------------------------------- helpers


class Director:
    def __init__(self, page):
        self.page = page
        self.t0 = time.monotonic()
        self.cues = []  # (offset seconds, wav path)
        self.chapter = ""

    async def say(self, idx, text, action=None, chapter=None):
        """Show the caption, start the narration clock, run the action, then hold until the line ends."""
        if chapter is not None:
            self.chapter = chapter
        start = time.monotonic()
        self.cues.append((start - self.t0, AUDIO[idx][0]))
        await self._caption(text)
        if action:
            await action()
            await self._caption(text)  # navigation may have happened
        remaining = AUDIO[idx][1] + 0.55 - (time.monotonic() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _caption(self, text):
        text = written(text)
        try:
            await self.page.evaluate("([c, h]) => window.__say && window.__say(c, h)", [text, self.chapter])
        except Exception:
            pass

    # ---- gestures
    async def point(self, selector, steps=28):
        loc = self.page.locator(selector).first
        try:
            if not await loc.count():
                return None
            await loc.scroll_into_view_if_needed(timeout=3000)
            box = await loc.bounding_box(timeout=3000)
        except Exception:
            return None
        if not box:
            return None
        x, y = box["x"] + min(box["width"] / 2, 120), box["y"] + box["height"] / 2
        await self.page.mouse.move(x, y, steps=steps)
        return x, y

    async def click(self, selector, pause=0.3):
        pos = await self.point(selector)
        await asyncio.sleep(pause)
        if pos:
            await self.page.mouse.click(*pos)
        else:
            await self.page.locator(selector).first.click(timeout=5000)
        await asyncio.sleep(0.4)

    async def type(self, selector, text):
        await self.click(selector)
        await self.page.keyboard.type(text, delay=55)

    async def select(self, selector, value):
        await self.point(selector)
        await asyncio.sleep(0.3)
        await self.page.locator(selector).first.select_option(value)
        await asyncio.sleep(0.3)

    async def scroll(self, dy, wait=1.0):
        await self.page.evaluate(f"window.scrollBy({{top:{dy},behavior:'smooth'}})")
        await asyncio.sleep(wait)

    async def top(self):
        await self.page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
        await asyncio.sleep(0.8)

    async def go(self, url):
        await self.page.goto(url, wait_until="load")
        await asyncio.sleep(0.4)


# spoken spellings (for the voice) -> written forms (for the captions)
WRITTEN = [
    ("accession archive dot vercel dot app", "accession-archive.vercel.app"),
    ("robots dot text", "robots.txt"), ("archive dot today", "archive.today"), ("archive dot org", "archive.org"),
    ("H T T P", "HTTP"), ("S M A", "SMA"), ("R S S", "RSS"), ("A P I", "API"), ("U T M", "UTM"), ("C S V", "CSV"),
    ("four oh four", "404"), ("five hundred", "500"), ("three oh four", "304"),
]


def written(text):
    for spoken, shown in WRITTEN:
        text = text.replace(spoken, shown)
    return text


def q(sql, *args):
    con = sqlite3.connect(DB, timeout=10)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


async def until(check, timeout=90, every=0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return True
        except sqlite3.Error:
            pass
        await asyncio.sleep(every)
    return False


def http_get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


# ----------------------------------------------------------------------------- servers

PROCS = {}


def start_app():
    env = dict(os.environ, ACCESSION_HOME=str(WORK / "home"))
    for k in ("VERCEL", "ACCESSION_SERVERLESS", "ACCESSION_PUBLIC", "ACCESSION_PASSWORD", "TURSO_DATABASE_URL"):
        env.pop(k, None)
    PROCS["app"] = subprocess.Popen([PY, "-m", "accession", "serve", "--port", "8010"], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            http_get(APP + "/api/health")
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("app did not start")


def start_servers():
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "home").mkdir(parents=True)
    (WORK / "audio").mkdir()
    for name, port in (("demo", 8765), ("demo2", 8766)):
        PROCS[name] = subprocess.Popen([PY, "-m", "accession", "demo-site", "--port", str(port), "--latency", "0.55"],
                                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # pace local snapshots so the archiving can be watched
    env = dict(os.environ, ACCESSION_HOME=str(WORK / "home"))
    subprocess.run([PY, "-c", "from accession import db; c = db.conn(); db.init_db(c); "
                    "db.set_setting(c, 'local_interval', '0.35'); db.set_setting(c, 'local_concurrency', '2')"],
                   cwd=ROOT, env=env, check=True)
    start_app()
    time.sleep(0.5)
    http_get(DEMO + "_demo/reset")


def stop_servers():
    for p in PROCS.values():
        if p.poll() is None:
            p.terminate()
    for p in PROCS.values():
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


# ----------------------------------------------------------------------------- narration

LINES = [
    # 1 - introduction
    ("01 · Introduction",
     "Welcome. This is Accession, a website archive submitter and automated backup repository, built for the "
     "S M A assignment. It is live on the web at accession archive dot vercel dot app, and the same code runs on a laptop."),
    (None,
     "The idea is simple. You give Accession a website. It discovers every public page on that site, cleans up the "
     "list, and submits each page to web archives such as the Internet Archive's Wayback Machine and archive dot today. "
     "It also keeps its own copy, and records everything in a permanent, searchable repository."),
    (None,
     "For this walkthrough I am running the same application on my laptop with an empty repository. This first page "
     "is the register, the dashboard. The ledger along the top shows the number of domains, URLs discovered, queued, "
     "submitted, archived, failed and pending, and the time of the last activity. These numbers update by themselves "
     "every two seconds."),
    # 2 - adding a domain
    ("02 · Adding a domain",
     "Let's add a website. I'm using a demonstration site that ships with the project. It contains every tricky case: "
     "a sitemap index, a compressed sitemap, an R S S feed, paginated blog pages, redirects, broken links, tracking "
     "parameters, and a folder that robots dot text forbids."),
    (None,
     "I type the address. You can paste one domain or many, one per line. That is how several websites are added at once."),
    (None,
     "Next, the archive services. The Wayback Machine uses the Internet Archive's Save Page Now A P I. archive dot today "
     "uses its public submission form. Local snapshot keeps a compressed copy inside the repository. This demo site only "
     "exists on my laptop, so the public archives cannot reach it. Here I'll use the local snapshot service."),
    (None,
     "The crawl settings set a page limit, how many clicks deep to follow links, what to do with links to other "
     "websites, and an optional rescan schedule. JavaScript rendering uses a headless Chromium browser for sites that "
     "build their links with scripts."),
    (None,
     "With start scanning now and automatic queueing switched on, I add the site to the register."),
    # 3 - discovery
    ("03 · Discovery",
     "The domain page opens and discovery starts immediately. The status stamp says scanning. The discovery bar shows "
     "how much of the crawl frontier is done, the pages fetched so far, and the crawl speed in pages per minute."),
    (None,
     "The activity log shows each step. The crawler first reads robots dot text, which gives the sitemap address and "
     "the paths it must not visit. It then follows the sitemap index to the plain and the compressed sitemaps, and reads "
     "the R S S feed."),
    (None,
     "At the same time it walks the pages, breadth first, collecting links, canonical tags, next and previous "
     "pagination, feed links and redirect targets. The crawl frontier lives in the database, not in memory. A site "
     "with hundreds of thousands of URLs works the same way, and nothing is lost if the server stops."),
    (None,
     "When the scan finishes, archiving starts by itself. Every new URL goes into the submission queue, and the "
     "archiving bar fills as the snapshots are saved."),
    # 4 - inventory
    ("04 · URL inventory",
     "This is the URL inventory. Each row is one page: its address, how it was found, its H T T P status, the scan that "
     "discovered it, and one column per archive service. A tick with a date links to the stored archive copy."),
    (None,
     "Filters make the inventory easy to explore. Filtering by sitemap shows the archive only pages. Nothing links to "
     "them, so only the sitemap could reveal them."),
    (None,
     "Filtering by pagination shows the older blog pages, found through next links and page numbers."),
    (None,
     "Filtering by status code shows the broken pages. The four oh four and the five hundred are recorded with their "
     "status, but they are not sent to the archives, because there is nothing worth preserving."),
    (None,
     "Some URLs are excluded, with a reason. The old about page redirects to the new one. The product list sorted by "
     "price is a duplicate according to its canonical tag. The drafts folder is disallowed by robots dot text. And the "
     "about link with U T M tracking parameters was normalized to the clean address, so it appears only once."),
    # 5 - url record
    ("05 · A URL's record",
     "Clicking a URL opens its full record. On the right is the repository entry: the normalized address, the original "
     "address exactly as first found, first and last seen times, H T T P status, content type, response time and a "
     "content fingerprint."),
    (None,
     "Found via lists every way the page was discovered: a normal link, the sitemap, and the redirect from the old "
     "about page."),
    (None,
     "On the left is the archive history. Each submission shows its service, state, reason, number of attempts and the "
     "archive link. Below that are the local snapshots, which can be viewed, shown as source, or compared."),
    (None,
     "Opening a snapshot shows the stored copy under a banner. Scripts are disabled, so an archived page can never run "
     "code inside the application."),
    # 6 - queue
    ("06 · The queue",
     "The queue page is the engine room. All domains share one combined queue. Important URLs go first. Otherwise the "
     "domains take turns, so one huge website cannot block the others."),
    (None,
     "Each archive service has its own pace. Save Page Now allows only a few captures per minute, so Accession spaces "
     "its requests and stays inside the published limits. If a service says it is overloaded, that service cools down "
     "for a while instead of retrying aggressively."),
    (None,
     "Temporary failures are retried with exponential backoff. Permanent errors, such as a page that no longer exists, "
     "fail immediately. Anything that needs a person ends up in needs review, including archive dot today's CAPTCHA "
     "pages and the Wayback Machine's login requirement. Accession never bypasses a CAPTCHA, a login or a rate limit. "
     "It offers a link to submit by hand, and the resulting archive address can be pasted back."),
    (None,
     "Further down are the slowest pages and the slowest captures, which point to slow URLs and slow services, and the "
     "list of workers with their heartbeats."),
    # 7 - search
    ("07 · Search",
     "The search page answers a simple question: has this page been kept? Pasting a full address gives a direct answer. "
     "Here it is yes, archived, with the latest archive link."),
    (None,
     "A partial search, combined with the service and submission filters, lists matching URLs across every domain in "
     "the repository."),
    # 8 - website changes
    ("08 · Website changes",
     "Websites change. To show this, I've published an update to the demo site: six new blog posts, an edited about "
     "page, and one post removed. Now I press scan again."),
    (None,
     "The scans tab compares the runs. Scan two found the new URLs and the one that is no longer linked, and it noticed "
     "which pages changed. Look at the three oh four column. Unchanged pages answered not modified, so they were not "
     "downloaded again. Accession reuses the links it stored the first time."),
    (None,
     "Only the new URLs were queued for archiving. Pages that were already archived were not sent again. That is the "
     "incremental backup rule: never submit the same URL twice without a reason."),
    (None,
     "When you do want a fresh copy, you ask for one. I request a new snapshot of the about page, then compare the two "
     "snapshots. The difference shows exactly the sentence that was edited."),
    # 9 - failure recovery
    ("09 · Crash and resume",
     "Now, failure recovery. I ask for fresh snapshots of every page, so there is work in the queue, and then I kill the "
     "server in the middle of it, exactly like a crash."),
    (None,
     "The server is gone, but the queue is safe in the database. Every item a worker takes is marked with that worker's "
     "name, and every worker sends a heartbeat every two seconds. I start the server again."),
    (None,
     "The new worker notices that the old one stopped sending heartbeats, and returns its unfinished items to the queue. "
     "The activity log reports what was recovered, and archiving carries on from where it stopped. Nothing is lost, "
     "and nothing is done twice."),
    # 10 - multiple domains
    ("10 · Multiple domains",
     "Accession handles many websites at once. I add a second site. The register now lists each domain with its own "
     "status and statistics, and both share the same combined queue."),
    (None,
     "On the queue page, the by domain table shows the sites taking turns. For more throughput, extra worker processes "
     "can run on other machines against the same database."),
    # 11 - system and export
    ("11 · Settings and export",
     "The system page holds the settings: the archive dot org keys the Wayback Machine needs, the pace of every "
     "service, retry limits and parallel scans. On the public website everyone can see this page, but only the owner "
     "can change it."),
    (None,
     "Finally, everything can be exported. The C S V and JSON exports contain every URL with its discovery source, "
     "timestamps, status, and the full history of its submissions and archive links."),
    # 12 - recap
    ("12 · Summary",
     "To recap. Accession discovers a site's public URLs from links, sitemaps, feeds and robots dot text. It normalizes "
     "and de-duplicates them. It submits them automatically through a persistent queue with retries and rate limits. It "
     "records everything in a searchable repository, detects new and changed pages on later scans, and recovers by "
     "itself after interruptions. The code, documentation and tests are on GitHub, and the site is live at accession "
     "archive dot vercel dot app. Thank you for watching."),
]

AUDIO = {}


def synth():
    for i, (_, text) in enumerate(LINES):
        aiff = WORK / "audio" / f"{i:02d}.aiff"
        wav = WORK / "audio" / f"{i:02d}.wav"
        subprocess.run(["say", "-v", VOICE, "-r", RATE, "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@44100", "-c", "1", str(aiff), str(wav)], check=True)
        with wave.open(str(wav)) as w:
            AUDIO[i] = (wav, w.getnframes() / w.getframerate())
    total = sum(d for _, d in AUDIO.values())
    print(f"narration: {len(LINES)} lines, {total / 60:.1f} min of speech")


def chap(i):
    return LINES[i][0]


# ----------------------------------------------------------------------------- the walkthrough

async def walkthrough(d: Director):
    p = d.page
    p.on("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))

    # 1 - introduction
    async def live():
        if LIVE_URL:
            try:
                await p.goto(LIVE_URL, wait_until="load", timeout=30000)
            except Exception:
                await d.go(APP + "/")
        else:
            await d.go(APP + "/")
    await d.say(0, LINES[0][1], live, chapter=chap(0))
    await d.say(1, LINES[1][1], lambda: d.scroll(500, 1.5))
    async def local_register():
        await d.go(APP + "/")
        await d.point(".ledger .v")
        await asyncio.sleep(1)
        await d.point(".ledger > div:nth-child(5) .v", steps=40)
    await d.say(2, LINES[2][1], local_register)

    # 2 - adding a domain
    async def open_drawer():
        if not await p.locator("details.drawer[open]").count():
            await d.click("details.drawer summary")
        await d.point("textarea[name=domains]")
    await d.say(3, LINES[3][1], open_drawer, chapter=chap(3))
    await d.say(4, LINES[4][1], lambda: d.type("textarea[name=domains]", DEMO))
    async def services():
        await d.point("input[value=wayback]")
        await asyncio.sleep(1.2)
        await d.click("input[value=wayback]")
        await asyncio.sleep(0.8)
        await d.point("input[value=archive_today]")
        await asyncio.sleep(1.2)
        await d.point("input[value=local]")
    await d.say(5, LINES[5][1], services)
    async def crawl_opts():
        for sel in ("input[name=max_pages]", "input[name=max_depth]", "select[name=external_mode]",
                    "input[name=rescan_hours]", "input[name=render_js]"):
            await d.point(sel)
            await asyncio.sleep(1.3)
        await d.select("select[name=external_mode]", "record")
    await d.say(6, LINES[6][1], crawl_opts)
    await d.say(7, LINES[7][1], lambda: d.click("button:has-text('Add to the register')"))

    # 3 - discovery
    async def watch_scan():
        await d.point("[data-bar=scan]")
        await asyncio.sleep(2)
        await d.point("[data-st=o-status]")
    await d.say(8, LINES[8][1], watch_scan, chapter=chap(8))
    await d.say(9, LINES[9][1], lambda: d.click("nav.tabs a:has-text('Activity')"))
    await d.say(10, LINES[10][1], lambda: d.scroll(250, 1.2))
    async def archive_starts():
        await until(lambda: q("SELECT COUNT(*) FROM scans WHERE state='done'")[0] >= 1, 90)
        await d.go(APP + "/domains/1")
        await d.point("[data-bar=archive]")
    await d.say(11, LINES[11][1], archive_starts)
    await until(lambda: q("SELECT COUNT(*) FROM submissions WHERE state IN ('queued','running','retry')")[0] == 0, 60)

    # 4 - inventory
    async def inventory():
        await d.go(APP + "/domains/1?tab=inventory")
        await d.scroll(330, 1.4)
        await d.point("table tbody tr:nth-child(2) td:nth-child(2)")
        await asyncio.sleep(1)
        await d.point("table tbody tr:nth-child(2) .cell a")
    await d.say(12, LINES[12][1], inventory, chapter=chap(12))
    async def f_sitemap():
        await d.select("select[name=source]", "sitemap")
        await d.click("button:has-text('Filter')")
        await d.scroll(300, 1.2)
    await d.say(13, LINES[13][1], f_sitemap)
    async def f_pagination():
        await d.select("select[name=source]", "pagination")
        await d.click("button:has-text('Filter')")
        await d.scroll(300, 1.2)
    await d.say(14, LINES[14][1], f_pagination)
    async def f_http():
        await d.select("select[name=source]", "")
        await d.select("select[name=http]", "4")
        await d.click("button:has-text('Filter')")
        await d.scroll(300, 1.0)
        await asyncio.sleep(1.5)
        await d.select("select[name=http]", "5")
        await d.click("button:has-text('Filter')")
        await d.scroll(300, 1.0)
    await d.say(15, LINES[15][1], f_http)
    async def f_excluded():
        await d.select("select[name=http]", "")
        await d.select("select[name=archive]", "skipped")
        await d.click("button:has-text('Filter')")
        await d.scroll(300, 1.0)
        for i in (1, 2, 3):
            await d.point(f"table tbody tr:nth-child({i}) .title-line.dim")
            await asyncio.sleep(1.6)
    await d.say(16, LINES[16][1], f_excluded)

    # 5 - url record
    about_id = q("SELECT id FROM urls WHERE url = ?", DEMO + "about")[0]
    async def url_page():
        await d.go(f"{APP}/urls/{about_id}")
        await d.point("dl.facts dd")
        await asyncio.sleep(1.2)
        await d.point("dl.facts dt:nth-of-type(2)", steps=20)
    await d.say(17, LINES[17][1], url_page, chapter=chap(17))
    await d.say(18, LINES[18][1], lambda: d.point("h2:has-text('Found via')"))
    async def history():
        await d.top()
        await d.point("h2:has-text('Archive history')")
        await asyncio.sleep(1.5)
        await d.point("h2:has-text('Local snapshots')")
    await d.say(19, LINES[19][1], history)
    snap = q("SELECT id FROM snapshots WHERE url_id = ? ORDER BY id DESC", about_id)[0]
    await d.say(20, LINES[20][1], lambda: d.go(f"{APP}/snapshots/{snap}"))

    # 6 - queue
    await d.say(21, LINES[21][1], lambda: d.go(APP + "/queue"), chapter=chap(21))
    async def services_table():
        await d.scroll(330, 1.2)
        await d.point("table tbody tr:nth-child(1) td:nth-child(3)")
        await asyncio.sleep(1.5)
        await d.point("table tbody tr:nth-child(1) td:nth-child(9)")
    await d.say(22, LINES[22][1], services_table)
    async def review():
        await d.page.locator("h2:has-text('Needs review')").scroll_into_view_if_needed()
        await d.point("h2:has-text('Needs review')")
    await d.say(23, LINES[23][1], review)
    async def slowest():
        await d.page.locator("h2:has-text('Slowest pages')").scroll_into_view_if_needed()
        await d.point("h2:has-text('Slowest pages')")
        await asyncio.sleep(1.5)
        await d.scroll(400, 1.2)
        await d.point("h2:has-text('Workers')")
    await d.say(24, LINES[24][1], slowest)

    # 7 - search
    async def search_exact():
        await d.go(APP + "/search")
        await d.type("input[name=q]", DEMO + "about")
        await p.keyboard.press("Enter")
        await p.wait_for_load_state("load")
        await d.point(".answer .yes")
    await d.say(25, LINES[25][1], search_exact, chapter=chap(25))
    async def search_partial():
        await p.locator("input[name=q]").fill("")
        await d.type("input[name=q]", "blog/post")
        await d.select("select[name=service]", "local")
        await d.select("select[name=state]", "success")
        await d.click("button:has-text('Search')")
        await d.scroll(350, 1.2)
    await d.say(26, LINES[26][1], search_partial)

    # 8 - website changes
    async def rescan():
        http_get(DEMO + "_demo/publish")
        await d.go(APP + "/domains/1")
        await d.click("button:has-text('Scan again')")
        await d.point("[data-bar=scan]")
    await d.say(27, LINES[27][1], rescan, chapter=chap(27))
    async def scans_tab():
        await until(lambda: q("SELECT COUNT(*) FROM scans WHERE domain_id = 1 AND state = 'done'")[0] >= 2, 90)
        await d.go(APP + "/domains/1?tab=scans")
        await d.point("table tbody tr:nth-child(1) td:nth-child(8)")
        await asyncio.sleep(1.4)
        await d.point("table tbody tr:nth-child(1) td:nth-child(9)")
        await asyncio.sleep(1.4)
        await d.point("table tbody tr:nth-child(1) td:nth-child(6)")
    await d.say(28, LINES[28][1], scans_tab)
    async def new_only():
        scan2 = q("SELECT id FROM scans WHERE domain_id = 1 ORDER BY id DESC")[0]
        await d.go(f"{APP}/domains/1?tab=inventory&scan={scan2}")
        await d.scroll(330, 1.4)
    await d.say(29, LINES[29][1], new_only)
    async def compare():
        await d.go(f"{APP}/urls/{about_id}")
        await d.click("button:has-text('Request a snapshot')")
        await until(lambda: q("SELECT COUNT(*) FROM snapshots WHERE url_id = ?", about_id)[0] >= 2, 30)
        await asyncio.sleep(0.5)
        await d.go(f"{APP}/urls/{about_id}")
        await d.click("a:has-text('Compare the latest two')")
        await d.scroll(260, 1.2)
        await d.point(".diff .add")
    await d.say(30, LINES[30][1], compare)

    # 9 - crash and resume
    async def load_and_kill():
        await d.go(APP + "/domains/1?tab=services")
        await d.select("select[name=scope]", "all")
        await d.click("button:has-text('Re-archive')")
        await until(lambda: q("SELECT COUNT(*) FROM submissions WHERE state = 'success' AND reason = 'rearchive'")[0] >= 6, 30)
        await d.go(APP + "/")
        await asyncio.sleep(1.5)
        PROCS["app"].send_signal(signal.SIGKILL)
        PROCS["app"].wait()
    await d.say(31, LINES[31][1], load_and_kill, chapter=chap(31))
    async def restart():
        await asyncio.sleep(2)
        start_app()
        await d.go(APP + "/")
    await d.say(32, LINES[32][1], restart)
    async def recovered():
        await until(lambda: q("SELECT COUNT(*) FROM events WHERE message LIKE '%recovered%' AND message NOT LIKE '0 submissions, 0 page%'")[0] >= 1, 45)
        await d.go(APP + "/")
        await d.point("[data-html=log] div:first-child")
    await d.say(33, LINES[33][1], recovered)

    # 10 - multiple domains
    async def second():
        await d.go(APP + "/")
        await d.click("details.drawer summary")
        await d.click("input[value=wayback]")
        await d.type("textarea[name=domains]", DEMO2)
        await d.click("button:has-text('Add to the register')")
        await asyncio.sleep(1.5)
        await d.go(APP + "/")
        await d.point("table tbody tr:nth-child(2) .domain-link")
    await d.say(34, LINES[34][1], second, chapter=chap(34))
    async def by_domain():
        await d.go(APP + "/queue")
        await d.page.locator("h2:has-text('By domain')").scroll_into_view_if_needed()
        await d.point("h2:has-text('By domain')")
    await d.say(35, LINES[35][1], by_domain)

    # 11 - system and export
    async def system():
        await d.go(APP + "/system")
        await d.point("input[name=ia_access_key]")
        await asyncio.sleep(1.5)
        await d.point("input[name=wayback_interval]")
    await d.say(36, LINES[36][1], system, chapter=chap(36))
    async def export():
        await d.go(APP + "/")
        await d.point("a:has-text('Export CSV')")
        await asyncio.sleep(1.4)
        await d.point("a:has-text('Export JSON')")
    await d.say(37, LINES[37][1], export)

    # 12 - summary
    async def finale():
        await d.go(APP + "/")
        await d.scroll(200, 1.0)
    await d.say(38, LINES[38][1], finale, chapter=chap(38))
    await asyncio.sleep(1.5)


async def record():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(WORK / "video"),
                                        record_video_size={"width": W, "height": H})
        await ctx.add_init_script(OVERLAY_JS)
        page = await ctx.new_page()
        d = Director(page)
        try:
            await walkthrough(d)
        finally:
            video = page.video
            await ctx.close()
            await browser.close()
        return Path(await video.path()), d.cues, time.monotonic() - d.t0


def build_audio(cues, total, path):
    rate = 44100
    frames = bytearray(int((total + 2) * rate) * 2)
    for offset, wav in cues:
        with wave.open(str(wav)) as w:
            data = w.readframes(w.getnframes())
        start = int(offset * rate) * 2
        end = min(start + len(data), len(frames))
        frames[start:end] = data[: end - start]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def main():
    start_servers()
    try:
        synth()
        webm, cues, total = asyncio.run(record())
    finally:
        stop_servers()
    voice = WORK / "voice.wav"
    build_audio(cues, total, voice)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-i", str(voice),
        "-vf", "scale=1920:1080:flags=lanczos,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-shortest", "-movflags", "+faststart", str(OUT),
    ], check=True)
    print(f"written {OUT} ({total / 60:.1f} min)")


if __name__ == "__main__":
    main()
