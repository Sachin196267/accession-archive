"""HTTP fetching with a size cap, timing and redirect bookkeeping."""
import ipaddress
import socket
import time
from dataclasses import dataclass, field

import httpx

from . import config

MAX_BODY = 8 * 1024 * 1024  # pages larger than this are recorded but not parsed


@dataclass
class FetchResult:
    url: str
    status: int | None = None  # status of the first response (301, 200, ...)
    final_status: int | None = None
    final_url: str | None = None
    redirects: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def ok(self) -> bool:
        return self.error is None and self.final_status is not None and self.final_status < 400

    def text(self) -> str:
        charset = "utf-8"
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip().strip('"') or "utf-8"
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


_host_ok: dict[str, bool] = {}


def is_public_host(host: str) -> bool:
    """True unless the host resolves to a private, loopback, link-local or reserved address."""
    host = (host or "").strip("[]").lower()
    if host in _host_ok:
        return _host_ok[host]
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True  # unresolvable: the request will fail on its own
    ok = all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)
    if len(_host_ok) > 5000:
        _host_ok.clear()
    _host_ok[host] = ok
    return ok


async def _refuse_internal(request: httpx.Request):
    # runs for every request, redirects included
    if not is_public_host(request.url.host):
        raise httpx.RequestError(f"refused: {request.url.host} is a private or internal address", request=request)


def make_client(timeout: float = 20, max_connections: int = 50) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        event_hooks={"request": [_refuse_internal]} if config.PUBLIC else None,
        follow_redirects=True,
        max_redirects=10,
        timeout=httpx.Timeout(timeout, connect=10),
        headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=20),
    )


async def fetch(client: httpx.AsyncClient, url: str, *, etag=None, last_modified=None, read_body=True) -> FetchResult:
    res = FetchResult(url=url)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    t0 = time.perf_counter()
    try:
        async with client.stream("GET", url, headers=headers) as r:
            res.final_status = r.status_code
            res.final_url = str(r.url)
            res.redirects = len(r.history)
            res.status = r.history[0].status_code if r.history else r.status_code
            res.headers = {k.lower(): v for k, v in r.headers.items()}
            if read_body and r.status_code != 304:
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BODY:
                        res.truncated = True
                        break
                res.body = b"".join(chunks)
    except httpx.TooManyRedirects:
        res.error = "too many redirects"
    except httpx.TimeoutException:
        res.error = "timed out"
    except httpx.ConnectError as e:
        res.error = f"connection failed: {_short(e)}"
    except httpx.HTTPError as e:
        res.error = f"{type(e).__name__}: {_short(e)}"
    res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return res


def _short(e: Exception) -> str:
    s = str(e) or type(e).__name__
    return s if len(s) < 160 else s[:157] + "..."
