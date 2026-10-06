"""A small sqlite3-compatible connection to a remote libSQL / Turso database.

Serverless hosts (Vercel) have no persistent disk, so the repository can live in a
hosted libSQL database instead of a local file. This speaks the Hrana-over-HTTP
protocol (`POST /v2/pipeline`) with nothing but httpx and exposes the subset of the
sqlite3 API the rest of the code uses: `execute`, `executescript`, cursors with
`fetchone` / `fetchall` / `rowcount` / `lastrowid`, and rows that work like
`sqlite3.Row` (index, key and `dict(row)` access).

Statements outside a transaction each run in a one-shot stream. `BEGIN` opens a
stream that is kept alive with its baton until `COMMIT` / `ROLLBACK`, so
`db.tx()` keeps its atomicity guarantees for queue claims.
"""
import base64
import re
import sqlite3

import httpx


class Row:
    __slots__ = ("_cols", "_vals", "_idx")

    def __init__(self, cols, vals, idx):
        self._cols, self._vals, self._idx = cols, vals, idx

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        try:
            return self._vals[self._idx[key]]
        except KeyError:
            raise IndexError(f"No item with that key: {key}") from None

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"Row({dict(zip(self._cols, self._vals))})"


class Cursor:
    def __init__(self, result=None):
        result = result or {}
        cols = [c.get("name") for c in result.get("cols", [])]
        idx = {name: i for i, name in enumerate(cols)}
        self._rows = [Row(cols, [_decode(v) for v in r], idx) for r in result.get("rows", [])]
        self.description = [(c, None, None, None, None, None, None) for c in cols] or None
        self.rowcount = result.get("affected_row_count", -1)
        rid = result.get("last_insert_rowid")
        self.lastrowid = int(rid) if rid is not None else None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        while self._rows:
            yield self._rows.pop(0)


class Connection:
    row_factory = None  # accepted for sqlite3 compatibility; rows are always Row objects

    def __init__(self, url: str, token: str | None = None, timeout: float = 30):
        url = url.strip()
        for scheme in ("libsql://", "wss://", "ws://"):
            if url.startswith(scheme):
                url = ("http://" if scheme == "ws://" else "https://") + url[len(scheme):]
        self.base = url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(timeout=timeout, headers=headers)
        self._baton = None
        self._stream_url = None
        self.in_transaction = False

    # ------------------------------------------------------------------ protocol

    def _pipeline(self, requests):
        body = {"baton": self._baton, "requests": requests}
        url = (self._stream_url or self.base) + "/v2/pipeline"
        attempts = 1 if self._baton else 3  # a fresh request is safe to resend if it never arrived
        for i in range(attempts):
            try:
                r = self._http.post(url, json=body)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if i == attempts - 1:
                    raise sqlite3.OperationalError(f"database unreachable at {self.base}") from None
        if r.status_code != 200:
            raise sqlite3.OperationalError(f"database HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        self._baton = data.get("baton")
        if data.get("base_url"):
            self._stream_url = data["base_url"].rstrip("/")
        out = []
        for res in data.get("results", []):
            if res.get("type") == "error":
                err = res.get("error", {})
                msg = err.get("message", "unknown database error")
                code = err.get("code", "") or ""
                exc = sqlite3.IntegrityError if "CONSTRAINT" in code or "constraint" in msg else sqlite3.OperationalError
                raise exc(msg)
            out.append(res.get("response", {}))
        return out

    def _reset_stream(self):
        self._baton = None
        self._stream_url = None
        self.in_transaction = False

    # ------------------------------------------------------------------ sqlite3 API

    def execute(self, sql, params=()):
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head == "PRAGMA":
            # connection pragmas (WAL, busy timeout, ...) are managed by the server
            return Cursor()
        begin = head == "BEGIN"
        end = head in ("COMMIT", "ROLLBACK", "END")
        if begin:
            self.in_transaction = True
        keep_open = self.in_transaction and not end
        requests = [{"type": "execute", "stmt": _stmt(sql, params)}]
        if not keep_open:
            requests.append({"type": "close"})
        try:
            resp = self._pipeline(requests)
        except Exception:
            if not keep_open or begin:
                self._reset_stream()
            if head == "ROLLBACK":
                return Cursor()  # the stream is gone, and with it the transaction
            raise
        if not keep_open:
            self._reset_stream()
        return Cursor(resp[0].get("result") if resp else None)

    def executescript(self, script):
        self._pipeline([{"type": "sequence", "sql": script}, {"type": "close"}])
        self._reset_stream()
        return Cursor()

    def commit(self):
        if self.in_transaction:
            self.execute("COMMIT")

    def rollback(self):
        if self.in_transaction:
            self.execute("ROLLBACK")

    def close(self):
        if self._baton:
            try:
                self._pipeline([{"type": "close"}])
            except Exception:
                pass
        self._reset_stream()
        self._http.close()


def _value(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode().rstrip("=")}
    return {"type": "text", "value": str(v)}


def _decode(v):
    t = v.get("type")
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "text":
        return v["value"]
    if t == "blob":
        b = v.get("base64", "")
        return base64.b64decode(b + "=" * (-len(b) % 4))
    return None


def _stmt(sql, params):
    stmt = {"sql": sql, "want_rows": True}
    if isinstance(params, dict):
        # sqlite3 ignores unused named parameters; libSQL servers reject them
        used = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", sql))
        stmt["named_args"] = [{"name": f":{k}", "value": _value(v)} for k, v in params.items() if k in used]
    elif params:
        stmt["args"] = [_value(v) for v in params]
    return stmt
