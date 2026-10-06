"""Background engine: runs scans, drains the submission queue, fires schedules.

The engine keeps no state that matters outside the database. It can run inside
the web server (default) or as one or more separate processes
(`python -m accession worker`), which then share the same queue - claims are
atomic and every claim carries the worker id plus a heartbeat, so a crashed
worker's items are picked up again by whoever is alive.
"""
import asyncio
import logging
import os
import socket
import threading
import time
import uuid

from . import config, db, render, repo
from .crawler import Scan
from .fetch import make_client
from .services import REGISTRY

log = logging.getLogger("accession.worker")


class Engine:
    def __init__(self, role: str = "all"):
        self.role = role  # all | crawl | archive
        self.worker_id = f"{socket.gethostname().split('.')[0]}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.stopping = False
        self.con = None
        self.client = None
        self.renderer = None
        self.settings: dict = {}
        self.services = {}
        self._scans: dict[int, asyncio.Task] = {}
        self._inflight: dict[str, set] = {}
        self._last_start: dict[str, float] = {}
        self._rr: dict[str, int] = {}
        self._thread = None
        self._loop = None
        self._done = threading.Event()

    # ------------------------------------------------------------------ control

    def start_in_thread(self):
        self._thread = threading.Thread(target=self._thread_main, name="accession-engine", daemon=True)
        self._thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self.main())
        except Exception:
            log.exception("engine crashed")
        finally:
            self._done.set()

    def stop(self, timeout=10):
        self.stopping = True
        if self._thread:
            self._done.wait(timeout)

    async def _nap(self, seconds):
        """Sleep that wakes up promptly when the engine is asked to stop."""
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            await asyncio.sleep(min(0.2, end - time.monotonic()))

    def save_job_id(self, sub_id, job_id):
        repo.save_job_id(self.con, sub_id, job_id)

    # ------------------------------------------------------------------ main loop

    async def main(self):
        self.con = db.connect()
        db.init_db(self.con)
        self.settings = db.settings(self.con)
        self.services = {name: cls(self) for name, cls in REGISTRY.items()}
        self.client = make_client(float(self.settings.get("fetch_timeout", 20)))
        if render.available():
            self.renderer = render.Renderer(config.USER_AGENT)
        self._register()
        repo.recover(self.con)
        log.info("worker %s started (role=%s)", self.worker_id, self.role)
        try:
            loops = [self._heartbeat_loop()]
            if self.role in ("all", "crawl"):
                loops += [self._scan_loop(), self._schedule_loop()]
            if self.role in ("all", "archive"):
                loops.append(self._dispatch_loop())
            await asyncio.gather(*loops)
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------ serverless bursts

    async def burst(self, seconds: float = 40, grace: float = 10) -> dict:
        """Work the queue for a bounded time, then hand everything back.

        Used where no process may outlive a request (Vercel). Each burst does exactly
        what the long-running engine does - scans, submissions, schedules - and stops
        early once there is nothing left to do. A lease in the settings table makes
        sure only one burst runs at a time, so service pacing stays correct.
        """
        self.con = db.connect()
        self.settings = db.settings(self.con)
        ttl = seconds + grace + 5
        if not self._take_lease(ttl):
            return {"ran": False, "reason": "busy"}
        t0 = time.monotonic()
        try:
            repo.recover(self.con)
            if not self._has_work():
                return {"ran": False, "reason": "idle"}
            self.services = {name: cls(self) for name, cls in REGISTRY.items()}
            self.client = make_client(min(float(self.settings.get("fetch_timeout", 20)), 15))
            if render.available():
                self.renderer = render.Renderer(config.USER_AGENT)
            self._register()

            async def watchdog():
                idle = 0
                while not self.stopping:
                    await self._nap(1)
                    if time.monotonic() - t0 > seconds:
                        break
                    busy = bool(self._scans) or any(not t.done() for ts in self._inflight.values() for t in ts)
                    idle = 0 if busy or self._has_work() else idle + 1
                    if idle >= 3:
                        break
                self.stopping = True

            await asyncio.gather(self._heartbeat_loop(), self._scan_loop(), self._schedule_loop(),
                                 self._dispatch_loop(), watchdog())
            # give captures that are already running a chance to finish
            pending = list(self._scans.values()) + [t for ts in self._inflight.values() for t in ts if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=grace)
            await self._shutdown()
        finally:
            self.con.execute("UPDATE settings SET value = '0' WHERE key = 'burst_until'")
        return {"ran": True, "seconds": round(time.monotonic() - t0, 1)}

    def _take_lease(self, ttl) -> bool:
        t = db.now()
        cur = self.con.execute(
            "UPDATE settings SET value = ? WHERE key = 'burst_until' AND CAST(value AS REAL) < ?", (str(t + ttl), t)
        )
        return cur.rowcount == 1

    def _has_work(self) -> bool:
        t = db.now()
        c = self.con
        if db.scalar(c, "SELECT EXISTS (SELECT 1 FROM scans WHERE state = 'running')"):
            return True
        if db.scalar(c, "SELECT EXISTS (SELECT 1 FROM domains WHERE rescan_hours IS NOT NULL AND next_scan_at <= ?)", t):
            return True
        if db.scalar(c, "SELECT value FROM settings WHERE key = 'queue_paused'") == "1":
            return False
        ready = [r["service"] for r in c.execute(
            "SELECT service FROM service_state WHERE enabled = 1 AND COALESCE(cooldown_until, 0) <= ?", (t,))]
        if not ready:
            return False
        marks = ",".join("?" * len(ready))
        return bool(db.scalar(
            c,
            f"""SELECT EXISTS (SELECT 1 FROM submissions s JOIN domains d ON d.id = s.domain_id
                WHERE d.paused = 0 AND s.service IN ({marks})
                  AND (s.state IN ('queued', 'running') OR (s.state = 'retry' AND s.next_attempt_at <= ?)))""",
            *ready, t,
        ))

    def _register(self):
        t = db.now()
        self.con.execute("DELETE FROM workers WHERE stopped_at IS NOT NULL AND stopped_at < ?", (t - 86400,))
        self.con.execute(
            """INSERT INTO workers (id, host, pid, role, started_at, heartbeat_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'running')""",
            (self.worker_id, socket.gethostname(), os.getpid(), self.role, t, t),
        )
        db.log(self.con, f"Worker {self.worker_id} started ({self.role})")

    async def _shutdown(self):
        for task in list(self._scans.values()):
            task.cancel()
        for tasks in self._inflight.values():
            for task in tasks:
                task.cancel()
        await asyncio.sleep(0)
        # hand everything this worker held back to the queue right away
        repo.recover(self.con, only_worker=self.worker_id)
        self.con.execute(
            "UPDATE workers SET stopped_at = ?, status = 'stopped' WHERE id = ?", (db.now(), self.worker_id)
        )
        if self.renderer:
            await self.renderer.close()
        await self.client.aclose()
        log.info("worker %s stopped", self.worker_id)

    async def _heartbeat_loop(self):
        last_recover = 0.0
        while not self.stopping:
            self.con.execute("UPDATE workers SET heartbeat_at = ? WHERE id = ?", (db.now(), self.worker_id))
            self.settings = db.settings(self.con)
            if time.monotonic() - last_recover > 15:
                last_recover = time.monotonic()
                repo.recover(self.con)
            await self._nap(2)

    # ------------------------------------------------------------------ scans

    async def _scan_loop(self):
        while not self.stopping:
            for sid, task in list(self._scans.items()):
                if task.done():
                    self._scans.pop(sid)
                    if task.exception():
                        db.log(self.con, f"Scan {sid} crashed: {task.exception()!r}", None, "error")
            limit = int(self.settings.get("max_parallel_scans", 4))
            if len(self._scans) < limit:
                t = db.now()
                rows = self.con.execute(
                    f"""SELECT id FROM scans WHERE state = 'running' AND
                           (worker IS NULL OR worker = ? OR lease_until < ? OR worker NOT IN ({repo.live_workers_sql()}))
                        ORDER BY id""",
                    (self.worker_id, t),
                ).fetchall()
                rows = [r for r in rows if r["id"] not in self._scans][: limit - len(self._scans)]
                for r in rows:
                    with db.tx(self.con):
                        self.con.execute(
                            "UPDATE scans SET worker = ?, lease_until = ? WHERE id = ?",
                            (self.worker_id, t + 120, r["id"]),
                        )
                    self._scans[r["id"]] = asyncio.create_task(self._run_scan(r["id"]))
            await self._nap(1)

    async def _run_scan(self, scan_id):
        try:
            await Scan(self, scan_id).run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            with db.tx(self.con):
                self.con.execute(
                    "UPDATE scans SET state = 'failed', finished_at = ?, note = ? WHERE id = ?",
                    (db.now(), f"{type(e).__name__}: {e}", scan_id),
                )
            log.exception("scan %s failed", scan_id)

    async def _schedule_loop(self):
        while not self.stopping:
            due = self.con.execute(
                "SELECT id, host FROM domains WHERE rescan_hours IS NOT NULL AND next_scan_at <= ?", (db.now(),)
            ).fetchall()
            for d in due:
                if repo.start_scan(self.con, d["id"], "schedule"):
                    db.log(self.con, "Scheduled rescan started", d["id"])
                self.con.execute(
                    "UPDATE domains SET next_scan_at = ? + rescan_hours * 3600 WHERE id = ?", (db.now(), d["id"])
                )
            await self._nap(20)

    # ------------------------------------------------------------------ submissions

    async def _dispatch_loop(self):
        while not self.stopping:
            if self.settings.get("queue_paused") != "1":
                states = {r["service"]: r for r in self.con.execute("SELECT * FROM service_state")}
                for name, svc in self.services.items():
                    st = states.get(name)
                    if st is not None and (not st["enabled"] or (st["cooldown_until"] or 0) > db.now()):
                        continue
                    self._start_some(name, svc)
            await self._nap(0.25)

    def _start_some(self, name, svc):
        concurrency, interval = svc.limits(self.settings)
        inflight = self._inflight.setdefault(name, set())
        inflight -= {t for t in inflight if t.done()}
        while len(inflight) < concurrency:
            if time.monotonic() - self._last_start.get(name, 0) < interval:
                return
            sub = repo.claim_submission(self.con, name, self.worker_id, self._rr.get(name, 0))
            if not sub:
                self._rr[name] = 0
                return
            self._rr[name] = sub["domain_id"]
            self._last_start[name] = time.monotonic()
            inflight.add(asyncio.create_task(self._submit(svc, sub)))

    async def _submit(self, svc, sub):
        from .services.base import ServiceResult

        try:
            res = await svc.submit(sub)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("submission %s crashed", sub["id"])
            res = ServiceResult("retry", error=f"internal error: {type(e).__name__}: {e}")
        if self.stopping and res.outcome == "retry":
            return  # leave it 'running'; shutdown hands it back without burning an attempt
        repo.finish_submission(self.con, sub, res, self.settings)
