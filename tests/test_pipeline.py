"""End-to-end: discovery, queue, services, incremental rescans and crash recovery."""
import time

from accession import db, repo
from accession.worker import Engine


def run_engine(con, timeout=60):
    eng = Engine()
    eng.start_in_thread()
    try:
        assert repo.wait_until_idle(con, timeout), "engine did not finish in time"
    finally:
        eng.stop(15)
    return eng


def fast_settings(con, wayback_base=None, today_host=None, keys=True):
    for k, v in {"wayback_interval": 0, "wayback_interval_auth": 0, "wayback_poll_interval": 0.05, "archive_today_interval": 0,
                 "retry_base_seconds": 0.1}.items():
        db.set_setting(con, k, v)
    if wayback_base:
        db.set_setting(con, "wayback_base", wayback_base)
    if keys:
        db.set_setting(con, "ia_access_key", "test-access")
        db.set_setting(con, "ia_secret_key", "test-secret")
    if today_host:
        db.set_setting(con, "archive_today_host", today_host)


def by_path(con, did):
    return {r["url"].split("/", 3)[3]: r for r in con.execute("SELECT * FROM urls WHERE domain_id = ? AND is_external = 0", (did,))}


def test_discovery_covers_every_source(home, demo_site):
    con = home
    did, _ = repo.add_domain(con, demo_site, services="local", external_mode="record")
    repo.start_scan(con, did)
    run_engine(con)
    urls = by_path(con, did)

    assert urls[""]["source"] == "seed"
    assert urls["blog/archive-only-1"]["source"] == "sitemap"  # only listed in the gzipped sitemap
    assert urls["blog/page/2/"]["source"] == "pagination"
    assert "feed.xml" in urls and urls["feed.xml"]["final_status"] == 200
    assert "about" in urls and "about?utm_medium=nav&utm_source=home" not in urls  # tracking params stripped
    assert urls["old-about"]["http_status"] == 301 and urls["old-about"]["skip_reason"].startswith("redirects")
    assert urls["missing-page"]["http_status"] == 404
    assert urls["server-error"]["http_status"] == 500
    assert urls["private/drafts"]["skip_reason"] == "disallowed by robots.txt"
    assert urls["products/?sort=price"]["skip_reason"].startswith("duplicate")
    assert not any(p.startswith("assets/") for p in urls)  # stylesheets are not pages
    ext = con.execute("SELECT * FROM urls WHERE is_external = 1").fetchall()
    assert [r["url"] for r in ext] == ["https://example.org/"] and ext[0]["skip_reason"]
    sources = {r["source"] for r in con.execute("SELECT source FROM url_sources")}
    assert {"seed", "link", "sitemap", "feed", "pagination", "canonical", "redirect", "external"} <= sources

    # only reachable, non-duplicate, in-scope pages were submitted - each exactly once
    subs = con.execute("SELECT u.url, s.state FROM submissions s JOIN urls u ON u.id = s.url_id").fetchall()
    sent = [r["url"] for r in subs]
    assert len(sent) == len(set(sent))
    assert all(r["state"] == "success" for r in subs)
    assert not any(p in u for u in sent for p in ("missing-page", "server-error", "private", "old-about", "sort=price"))
    assert db.scalar(con, "SELECT COUNT(*) FROM snapshots") == len(sent)


def test_second_scan_is_incremental(home, demo_site):
    from accession import demo_site as ds

    con = home
    did, _ = repo.add_domain(con, demo_site, services="local")
    repo.start_scan(con, did)
    run_engine(con)
    first_total = db.scalar(con, "SELECT COUNT(*) FROM submissions")

    ds.STATE["version"] = 2  # six new posts, /about edited, post-3 removed
    repo.start_scan(con, did)
    run_engine(con)
    scan = db.one(con, "SELECT * FROM scans WHERE domain_id = ? ORDER BY id DESC", did)
    assert scan["number"] == 2
    assert scan["urls_new"] >= 6 and scan["urls_missing"] == 1
    assert scan["not_modified"] > 20  # unchanged pages answered 304 and were not downloaded again
    assert scan["urls_changed"] >= 1

    new_subs = db.scalar(con, "SELECT COUNT(*) FROM submissions") - first_total
    assert new_subs == db.scalar(con, "SELECT COUNT(*) FROM urls WHERE first_scan_id = ? AND skip_reason IS NULL "
                                      "AND (final_status IS NULL OR final_status < 400)", scan["id"])
    # explicit re-archive adds history instead of replacing it
    about = by_path(con, did)["about"]
    repo.enqueue_rearchive(con, did, ["local"], url_ids=[about["id"]])
    run_engine(con)
    hist = con.execute("SELECT * FROM submissions WHERE url_id = ? ORDER BY id", (about["id"],)).fetchall()
    assert [h["state"] for h in hist] == ["success", "success"] and hist[1]["reason"] == "rearchive"


def test_wayback_and_archive_today_integrations(home, demo_site, archives):
    base, mock = archives
    con = home
    fast_settings(con, wayback_base=base, today_host=base)
    did, _ = repo.add_domain(con, demo_site, services="wayback,archive_today", max_pages=5, max_depth=1)
    repo.start_scan(con, did)
    run_engine(con)

    rows = con.execute("SELECT s.*, u.url FROM submissions s JOIN urls u ON u.id = s.url_id "
                       "WHERE service = 'wayback'").fetchall()
    # with a 5-page limit some URLs were never fetched, so the 404 page reaches the service, which rejects it
    wb = [s for s in rows if "missing" not in s["url"]]
    assert all(s["state"] == "failed" for s in rows if "missing" in s["url"])
    assert wb and all(s["state"] == "success" for s in wb)
    assert all(s["archive_url"].startswith(f"{base}/web/20261004120000/") for s in wb)
    assert all(s["job_id"].startswith("spn2-") for s in wb)
    at = con.execute("SELECT * FROM submissions WHERE service = 'archive_today'").fetchall()
    assert all(s["state"] == "success" and s["archive_url"] == f"{base}/AbC12" for s in at)
    assert any(p == "/submit/" and f.get("submitid") == "tok123" for p, f, _ in mock.calls)
    assert all(h.get("Authorization") == "LOW test-access:test-secret" for p, f, h in mock.calls if p == "/save")


def test_wayback_without_keys(home, demo_site, archives):
    base, mock = archives
    con = home
    fast_settings(con, wayback_base=base, keys=False)
    did, _ = repo.add_domain(con, demo_site, services="wayback", max_pages=2, max_depth=0)
    repo.start_scan(con, did)
    run_engine(con)
    s = db.one(con, "SELECT * FROM submissions WHERE state = 'success'")
    assert s["archive_url"].startswith(f"{base}/web/20261004130000/") and s["note"] == "anonymous Save Page Now"

    # when the Archive insists on a login, the item waits for a human instead of looping
    mock.login_wall = True
    repo.enqueue_rearchive(con, did, ["wayback"])
    eng = Engine()
    eng.start_in_thread()
    deadline = time.time() + 20
    while time.time() < deadline and not db.scalar(con, "SELECT COUNT(*) FROM submissions WHERE state = 'blocked'"):
        time.sleep(0.2)
    eng.stop(15)
    blocked = db.one(con, "SELECT * FROM submissions WHERE state = 'blocked'")
    assert blocked and "login" in blocked["error"] and blocked["manual_url"].startswith("https://web.archive.org/save/")
    assert db.scalar(con, "SELECT cooldown_until FROM service_state WHERE service = 'wayback'") > time.time() + 3000


def test_captcha_is_never_bypassed(home, demo_site, archives):
    base, mock = archives
    mock.captcha = True
    con = home
    fast_settings(con, today_host=base)
    did, _ = repo.add_domain(con, demo_site, services="archive_today", max_pages=3, max_depth=0)
    repo.start_scan(con, did)
    eng = Engine()
    eng.start_in_thread()
    deadline = time.time() + 30
    while time.time() < deadline and not db.scalar(con, "SELECT COUNT(*) FROM submissions WHERE state = 'blocked'"):
        time.sleep(0.2)
    eng.stop(15)
    blocked = db.one(con, "SELECT * FROM submissions WHERE state = 'blocked'")
    assert blocked and "CAPTCHA" in blocked["error"] and blocked["manual_url"].startswith(base)
    st = db.one(con, "SELECT * FROM service_state WHERE service = 'archive_today'")
    assert st["cooldown_until"] > time.time() + 600  # the service backs off instead of retrying
    # the user submits by hand and records the link
    repo.record_manual(con, blocked["id"], "https://archive.ph/XyZ98")
    assert db.one(con, "SELECT state, archive_id FROM submissions WHERE id = ?", blocked["id"])["archive_id"] == "XyZ98"


def test_permanent_and_temporary_failures(home, archives, demo_site):
    base, _ = archives
    con = home
    fast_settings(con, wayback_base=base)
    db.set_setting(con, "max_attempts", 2)
    did, _ = repo.add_domain(con, demo_site, services="wayback")
    with db.tx(con):
        scan = con.execute("INSERT INTO scans (domain_id, number, state, started_at) VALUES (?, 1, 'done', ?)",
                           (did, time.time())).lastrowid
        for path in ("ok-page", "missing-page"):
            repo.record_url(con, did, scan, demo_site + path, demo_site + path, "seed")
    repo.enqueue_new(con, did)
    run_engine(con)
    rows = {r["url"].rsplit("/", 1)[1]: r for r in con.execute(
        "SELECT s.*, u.url FROM submissions s JOIN urls u ON u.id = s.url_id")}
    assert rows["ok-page"]["state"] == "success"
    assert rows["missing-page"]["state"] == "failed" and "error:not-found" in rows["missing-page"]["error"]
    assert rows["missing-page"]["attempts"] == 1  # permanent errors are not retried


def test_crash_recovery_resumes_from_last_state(home, demo_site):
    con = home
    did, _ = repo.add_domain(con, demo_site, services="local", auto_archive=0)
    repo.start_scan(con, did)
    run_engine(con)
    repo.enqueue_new(con, did)

    # a worker claims work and then dies without a word (kill -9)
    t = time.time()
    con.execute("INSERT INTO workers (id, started_at, heartbeat_at, status) VALUES ('dead', ?, ?, 'running')",
                (t - 100, t - 100))
    held = [repo.claim_submission(con, "local", "dead")["id"] for _ in range(3)]
    assert db.scalar(con, "SELECT COUNT(*) FROM submissions WHERE state = 'running'") == 3

    run_engine(con)  # a fresh worker notices the stale heartbeat and takes over
    states = {r["state"] for r in con.execute(f"SELECT state FROM submissions WHERE id IN ({','.join(map(str, held))})")}
    assert states == {"success"}
    assert db.scalar(con, "SELECT COUNT(*) FROM submissions WHERE state != 'success'") == 0
    assert db.one(con, "SELECT status FROM workers WHERE id = 'dead'")["status"] == "lost"


def test_interrupted_scan_resumes(home, demo_site):
    con = home
    did, _ = repo.add_domain(con, demo_site, services="local", auto_archive=0, crawl_concurrency=1)
    sid = repo.start_scan(con, did)
    eng = Engine()
    eng.start_in_thread()
    while db.scalar(con, "SELECT pages_fetched FROM scans WHERE id = ?", sid) < 5:
        time.sleep(0.01)
    eng.stop(15)  # graceful stop in the middle of the crawl
    mid = db.one(con, "SELECT * FROM scans WHERE id = ?", sid)
    if mid["state"] == "running":
        assert db.scalar(con, "SELECT COUNT(*) FROM frontier WHERE scan_id = ? AND state = 'leased'", sid) == 0
    run_engine(con)
    done = db.one(con, "SELECT * FROM scans WHERE id = ?", sid)
    assert done["state"] == "done" and done["urls_seen"] >= 45
    # no page was downloaded twice in the same scan
    assert db.scalar(con, "SELECT COUNT(*) FROM frontier WHERE scan_id = ? GROUP BY url, kind HAVING COUNT(*) > 1", sid) is None


def test_round_robin_between_domains(home):
    con = home
    ids = []
    for host in ("a.test", "b.test", "c.test"):
        did, _ = repo.add_domain(con, host, services="local")
        ids.append(did)
        for i in range(5):
            repo.record_url(con, did, None, f"https://{host}/{i}", f"https://{host}/{i}", "seed")
        repo.enqueue_new(con, did)
    order, last = [], 0
    for _ in range(6):
        s = repo.claim_submission(con, "local", "w", last)
        order.append(s["domain_id"])
        last = s["domain_id"]
    assert order == ids + ids  # domains take turns in the combined queue
    # a priority URL jumps ahead of everyone
    u = db.one(con, "SELECT id FROM urls WHERE url = 'https://c.test/4'")
    con.execute("UPDATE submissions SET priority = 10 WHERE url_id = ?", (u["id"],))
    assert repo.claim_submission(con, "local", "w", ids[0])["url_id"] == u["id"]


def test_javascript_rendering_finds_script_links(home, demo_site):
    import pytest

    from accession import render

    if not render.available():
        pytest.skip("Playwright not installed")
    con = home
    did, _ = repo.add_domain(con, demo_site + "app/", services="local", render_js=1, max_depth=1, auto_archive=0)
    repo.start_scan(con, did)
    run_engine(con, timeout=120)
    urls = by_path(con, did)
    for view in ("dashboard", "layers", "history"):
        assert urls[f"app/{view}"]["source"] == "js-render"  # absent from the raw HTML
    scan = db.one(con, "SELECT * FROM scans WHERE domain_id = ?", did)
    assert "only visible after JavaScript rendering" in scan["note"]
    # the local snapshot keeps the rendered DOM, including the script-made links
    repo.enqueue_rearchive(con, did, ["local"], url_ids=[urls["app/"]["id"]])
    run_engine(con, timeout=120)
    from accession.services.local import read_snapshot

    snap = db.one(con, "SELECT * FROM snapshots WHERE url_id = ?", urls["app/"]["id"])
    assert snap["rendered"] == 1 and b'href="/app/layers"' in read_snapshot(snap)


def test_serverless_bursts_finish_the_job(home, demo_site):
    """On Vercel nothing outlives a request: short bursts must add up to a full run."""
    import asyncio

    con = home
    did, _ = repo.add_domain(con, demo_site, services="local")
    repo.start_scan(con, did)
    bursts = 0
    while bursts < 20:
        res = asyncio.run(Engine().burst(seconds=0.6, grace=2))
        if not res["ran"]:
            assert res["reason"] == "idle"
            break
        bursts += 1
    assert bursts >= 2  # the work really was split across bursts
    assert db.one(con, "SELECT state FROM scans WHERE domain_id = ?", did)["state"] == "done"
    assert db.scalar(con, "SELECT COUNT(*) FROM submissions WHERE state != 'success'") == 0
    assert db.scalar(con, "SELECT COUNT(*) FROM submissions") >= 40

    # only one burst may run at a time
    eng = Engine()
    eng.con = con
    assert eng._take_lease(30) and not Engine()._take_lease.__func__(eng, 30)
    con.execute("UPDATE settings SET value = '0' WHERE key = 'burst_until'")
