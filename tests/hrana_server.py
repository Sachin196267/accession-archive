"""Minimal Hrana-over-HTTP server backed by sqlite3, for testing accession.remote.

It implements the parts of the protocol Turso/libSQL servers expose that the
client uses: POST /v2/pipeline with execute / sequence / close requests and
batons that keep a stream (and its open transaction) alive between requests.
"""
import base64
import json
import re
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _dec(v):
    t = v["type"]
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "text":
        return v["value"]
    if t == "blob":
        b = v["base64"]
        return base64.b64decode(b + "=" * (-len(b) % 4))
    return None


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode().rstrip("=")}
    return {"type": "text", "value": v}


class _Handler(BaseHTTPRequestHandler):
    db_path = ""
    token = None
    streams: dict = {}
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v2/pipeline":
            return self._json(404, {"message": "not found"})
        if self.token and self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._json(401, {"message": "unauthorized"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        baton = body.get("baton")
        if baton:
            with self.lock:
                conn = self.streams.pop(baton, None)
            if conn is None:
                return self._json(400, {"message": "stream expired"})
        else:
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=30000")
        results, closed = [], False
        for req in body["requests"]:
            try:
                if req["type"] == "execute":
                    st = req["stmt"]
                    named = {n["name"][1:]: _dec(n["value"]) for n in st.get("named_args", [])}
                    used = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", st["sql"]))
                    if named and set(named) != used:  # strict like libSQL servers
                        raise sqlite3.OperationalError(
                            f"Input error: Number of arguments mismatch: expected {len(used)}, got {len(named)}")
                    cur = conn.execute(st["sql"], named or [_dec(a) for a in st.get("args", [])])
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description or []]
                    results.append({"type": "ok", "response": {"type": "execute", "result": {
                        "cols": [{"name": c, "decltype": None} for c in cols],
                        "rows": [[_enc(v) for v in r] for r in rows],
                        "affected_row_count": max(cur.rowcount, 0),
                        "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None,
                    }}})
                elif req["type"] == "sequence":
                    conn.executescript(req["sql"])
                    results.append({"type": "ok", "response": {"type": "sequence"}})
                elif req["type"] == "close":
                    closed = True
                    results.append({"type": "ok", "response": {"type": "close"}})
            except sqlite3.Error as e:
                code = "SQLITE_CONSTRAINT" if isinstance(e, sqlite3.IntegrityError) else "SQLITE_ERROR"
                results.append({"type": "error", "error": {"message": str(e), "code": code}})
        new_baton = None
        if closed:
            conn.close()
        else:
            new_baton = uuid.uuid4().hex
            with self.lock:
                self.streams[new_baton] = conn
        self._json(200, {"baton": new_baton, "base_url": None, "results": results})


def serve(db_path, port, token=None):
    handler = type("Handler", (_Handler,), {"db_path": str(db_path), "token": token, "streams": {}})
    sqlite3.connect(db_path).execute("PRAGMA journal_mode=WAL").close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
