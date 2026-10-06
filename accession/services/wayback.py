"""Internet Archive - Wayback Machine, via Save Page Now 2 (SPN2).

Mechanism (documented SPN2 API):
  POST {base}/save                       url=<target>  -> {"url", "job_id"}
  GET  {base}/save/status/<job_id>                     -> {"status": pending|success|error, ...}

Authenticated requests send `Authorization: LOW <access>:<secret>` (archive.org S3
keys). This is the reliable path: the status endpoint answers "You need to be
logged in to use Save Page Now" to anonymous clients (checked October 2026).

Without keys the tool falls back to the plain `GET {base}/save/<url>` form, which
answers with a redirect / Content-Location pointing at the new capture when
anonymous saving is allowed. When the Archive asks for a login instead, the
submission is marked "blocked" with a link for saving it by hand, and the service
pauses for an hour rather than hammering the endpoint.

Rate-limit responses pause the service instead of pushing harder, and the job id
is stored as soon as it is issued so an interrupted worker resumes polling the
same job instead of capturing twice.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .. import config
from .base import ArchiveService, ServiceResult

# status_ext values that will not succeed on retry
PERMANENT = {
    "error:not-found", "error:no-access", "error:unauthorized", "error:blocked-url",
    "error:blocked", "error:invalid-url-syntax", "error:filesize-limit", "error:too-many-redirects",
    "error:method-not-allowed", "error:ftp-access-denied", "error:proxy-error",
}
# status_ext values that mean "slow down" for the whole service
COOLDOWN = {
    "error:too-many-requests": 120,
    "error:user-session-limit": 90,
    "error:max-daily-bandwidth": 6 * 3600,
    "error:max-daily-bandwidth-from-ip": 6 * 3600,
    "error:blocked-client-ip": 24 * 3600,
}
JOB_RE = re.compile(r"""spn\.watchJob\(["']([^"']+)["']""")
CAPTURE_RE = re.compile(r"/web/(\d{14})/")
LOGIN_RE = re.compile(r"logged in|log in to|login required|sign in to", re.I)


class Wayback(ArchiveService):
    name = "wayback"
    label = "Wayback Machine"
    description = "Internet Archive Save Page Now 2"

    def _keys(self, settings):
        return config.ia_keys(settings)

    def limits(self, settings):
        authed = all(self._keys(settings))
        interval = settings.get("wayback_interval_auth" if authed else "wayback_interval", 20)
        return int(settings.get("wayback_concurrency", 1)), float(interval)

    def configured(self, settings):
        if all(self._keys(settings)):
            return True, "authenticated with archive.org keys"
        return False, "anonymous - add archive.org keys on the System page"

    @staticmethod
    def browse_url(url):
        return f"https://web.archive.org/web/*/{url}"

    def _headers(self, settings):
        h = {"Accept": "application/json"}
        access, secret = self._keys(settings)
        if access and secret:
            h["Authorization"] = f"LOW {access}:{secret}"
        return h

    async def submit(self, sub) -> ServiceResult:
        s = self.engine.settings
        base = s.get("wayback_base", "https://web.archive.org").rstrip("/")
        client: httpx.AsyncClient = self.engine.client
        headers = self._headers(s)
        target = sub["url"]

        reuse_days = float(s.get("wayback_reuse_days") or 0)
        if reuse_days > 0 and sub["reason"] == "new":
            found = await self._recent_capture(client, target, reuse_days)
            if found:
                return found

        if "Authorization" not in headers and not sub["job_id"]:
            return await self._anonymous(client, base, target)

        job_id = sub["job_id"]
        if not job_id:
            try:
                r = await client.post(
                    f"{base}/save", data={"url": target, "skip_first_archive": "1"},
                    headers=headers, timeout=60, follow_redirects=False,
                )
            except httpx.HTTPError as e:
                return ServiceResult("retry", error=f"save request failed: {type(e).__name__}")
            if r.status_code == 429:
                return ServiceResult("retry", http_status=429, error="rate limited by Save Page Now",
                                     cooldown=_retry_after(r, 120), retry_after=_retry_after(r, 120))
            if r.status_code in (401, 403):
                return ServiceResult("failed", http_status=r.status_code,
                                     error="Save Page Now rejected the request (check the archive.org keys)")
            if r.status_code >= 500:
                return ServiceResult("retry", http_status=r.status_code, error=f"Save Page Now returned HTTP {r.status_code}")
            data = _json(r)
            job_id = data.get("job_id") if data else None
            if not job_id:
                m = JOB_RE.search(r.text)
                job_id = m.group(1) if m else None
            if not job_id:
                if data and data.get("status") == "error":
                    return self._error(data, target)
                msg = (data or {}).get("message") or _first_line(r.text)
                return ServiceResult("retry", http_status=r.status_code, error=f"no job id returned: {msg}")
            self.engine.save_job_id(sub["id"], job_id)

        return await self._poll(client, base, headers, job_id, target, float(s.get("wayback_poll_timeout", 240)),
                                float(s.get("wayback_poll_interval", 3)))

    async def _poll(self, client, base, headers, job_id, target, timeout, delay=3.0) -> ServiceResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 15)
            try:
                r = await client.get(f"{base}/save/status/{job_id}", headers=headers, timeout=30)
            except httpx.HTTPError:
                continue
            if r.status_code == 429:
                await asyncio.sleep(_retry_after(r, 20))
                continue
            data = _json(r)
            if not data:
                continue
            status = data.get("status")
            if not status and LOGIN_RE.search(data.get("message", "")):
                return self._login_wall(target)
            if status == "pending":
                continue
            if status == "success":
                ts = data.get("timestamp")
                original = data.get("original_url") or target
                return ServiceResult(
                    "success",
                    archive_url=f"{base}/web/{ts}/{original}",
                    archive_id=ts,
                    http_status=data.get("http_status"),
                    job_id=job_id,
                    note=f"SPN2 job {job_id}, {data.get('duration_sec', '?')}s",
                )
            if status == "error":
                return self._error(data, target)  # job_id cleared: a retry asks for a new capture
        return ServiceResult("retry", job_id=job_id, error="capture still pending; will poll the same job again",
                             retry_after=60)

    async def _anonymous(self, client, base, target) -> ServiceResult:
        try:
            r = await client.get(f"{base}/save/{target}", timeout=150, follow_redirects=False,
                                 headers={"Accept": "text/html,application/json"})
        except httpx.TimeoutException:
            return ServiceResult("retry", error="Save Page Now did not answer within 150 s")
        except httpx.HTTPError as e:
            return ServiceResult("retry", error=f"save request failed: {type(e).__name__}")
        loc = r.headers.get("content-location") or r.headers.get("location") or ""
        m = CAPTURE_RE.search(loc)
        if m:
            return ServiceResult("success", archive_url=f"{base}/web/{m.group(1)}/{target}", archive_id=m.group(1),
                                 http_status=r.status_code, note="anonymous Save Page Now")
        if r.status_code == 429:
            return ServiceResult("retry", http_status=429, error="rate limited by Save Page Now",
                                 cooldown=_retry_after(r, 120), retry_after=_retry_after(r, 120))
        if r.status_code in (401, 403) or LOGIN_RE.search(r.text[:20000]):
            return self._login_wall(target, r.status_code)
        job = JOB_RE.search(r.text)
        if job:
            return await self._poll(client, base, {"Accept": "application/json"}, job.group(1), target,
                                    float(self.engine.settings.get("wayback_poll_timeout", 240)),
                                    float(self.engine.settings.get("wayback_poll_interval", 3)))
        if r.status_code >= 500:
            return ServiceResult("retry", http_status=r.status_code, error=f"Save Page Now returned HTTP {r.status_code}")
        return ServiceResult("retry", http_status=r.status_code,
                             error=f"no capture link in the answer: {_first_line(r.text)}")

    def _login_wall(self, target, status=None) -> ServiceResult:
        return ServiceResult(
            "blocked", http_status=status, manual_url=f"https://web.archive.org/save/{target}", cooldown=3600,
            error="Save Page Now asked for an archive.org login - add S3 keys on the System page or save it by hand",
        )

    def _error(self, data, target) -> ServiceResult:
        ext = data.get("status_ext") or ""
        msg = data.get("message") or ext or "unknown error"
        if ext in COOLDOWN:
            return ServiceResult("retry", error=f"{ext}: {msg}", cooldown=COOLDOWN[ext], retry_after=COOLDOWN[ext])
        if ext == "error:too-many-daily-captures":
            return ServiceResult("retry", error=f"{ext}: {msg}", retry_after=12 * 3600)
        if ext in PERMANENT:
            return ServiceResult("failed", error=f"{ext}: {msg}")
        if not ext and "not found" in msg.lower():
            return ServiceResult("retry", error="SPN2 job expired; a new capture will be requested")
        return ServiceResult("retry", error=f"{ext or 'error'}: {msg}")

    async def _recent_capture(self, client, target, days) -> ServiceResult | None:
        """Reuse an existing capture newer than `days` instead of requesting another."""
        try:
            r = await client.get("https://archive.org/wayback/available", params={"url": target}, timeout=20)
            snap = (_json(r) or {}).get("archived_snapshots", {}).get("closest")
        except httpx.HTTPError:
            return None
        if not snap or not snap.get("available"):
            return None
        ts = snap.get("timestamp", "")
        try:
            when = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
        if time.time() - when > days * 86400:
            return None
        return ServiceResult("success", archive_url=snap.get("url"), archive_id=ts,
                             http_status=int(snap.get("status") or 0) or None,
                             note=f"existing capture from {ts[:8]} reused")


def _json(r):
    try:
        data = r.json()
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _retry_after(r, default):
    try:
        return max(float(r.headers.get("retry-after", default)), 5)
    except ValueError:
        return default


def _first_line(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = " ".join(text.split())
    return text[:140] or "empty response"


def availability_url(url):
    return f"https://archive.org/wayback/available?url={quote(url, safe='')}"
