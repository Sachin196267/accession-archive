import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the repository at a fresh temporary directory.

    With ACCESSION_TEST_DB=remote the same tests run against a hosted-database
    stand-in (Hrana over HTTP), the way the app runs on Vercel with Turso.
    """
    from accession import config, db

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "accession.db")
    monkeypatch.setattr(config, "SNAPSHOT_DIR", tmp_path / "snapshots")
    httpd = None
    if os.environ.get("ACCESSION_TEST_DB") == "remote":
        from hrana_server import serve

        port = free_port()
        httpd = serve(tmp_path / "remote.db", port, token="test-token")
        monkeypatch.setattr(config, "DB_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(config, "DB_TOKEN", "test-token")
        monkeypatch.setattr(config, "SNAPSHOTS_IN_DB", True)
    else:
        monkeypatch.setattr(config, "DB_URL", "")
    con = db.conn()
    db.init_db(con)
    yield con
    if httpd:
        httpd.shutdown()


@pytest.fixture()
def demo_site():
    from accession import demo_site as ds

    ds.STATE["version"] = 1
    port = free_port()
    httpd = ds.serve(port, background=True)
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()
    ds.STATE["version"] = 1


class _MockArchives(BaseHTTPRequestHandler):
    """Speaks just enough SPN2 and archive.today for the integration tests."""

    jobs: dict = {}
    calls: list = []
    captcha = False
    login_wall = False

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        type(self).calls.append((self.path, form, dict(self.headers)))
        if self.path == "/save":
            job = f"spn2-{len(self.jobs) + 1:04d}"
            self.jobs[job] = {"url": form["url"], "polls": 0}
            return self._send(200, json.dumps({"url": form["url"], "job_id": job}))
        if self.path == "/submit/":
            if self.captcha:
                return self._send(429, '<div class="g-recaptcha"></div>', "text/html")
            return self._send(302, "", "text/html", {"Location": "/wip/AbC12"})
        self._send(404, "{}")

    def do_GET(self):
        if self.path.startswith("/save/http"):  # anonymous, synchronous save
            target = self.path[len("/save/"):]
            type(self).calls.append((self.path, {}, dict(self.headers)))
            if self.login_wall:
                return self._send(200, "<p>You need to be logged in to use Save Page Now.</p>", "text/html")
            return self._send(302, "", "text/html", {"Location": f"/web/20261004130000/{target}"})
        if self.path.startswith("/save/status/"):
            job = self.jobs.get(self.path.rsplit("/", 1)[1])
            if not job:
                return self._send(200, json.dumps({"status": "error", "message": "Job not found"}))
            job["polls"] += 1
            if job["polls"] < 2:
                return self._send(200, json.dumps({"status": "pending"}))
            if "missing" in job["url"]:
                return self._send(200, json.dumps({"status": "error", "status_ext": "error:not-found",
                                                   "message": "The server responded 404"}))
            return self._send(200, json.dumps({"status": "success", "timestamp": "20261004120000",
                                               "original_url": job["url"], "duration_sec": 1.2}))
        if self.path == "/":
            if self.captcha:
                return self._send(429, "<title>captcha</title><div class='h-captcha'></div>", "text/html")
            return self._send(200, '<form><input type="hidden" name="submitid" value="tok123"></form>', "text/html")
        self._send(404, "{}")


@pytest.fixture()
def archives():
    _MockArchives.jobs = {}
    _MockArchives.calls = []
    _MockArchives.captcha = False
    _MockArchives.login_wall = False
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _MockArchives)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", _MockArchives
    httpd.shutdown()
