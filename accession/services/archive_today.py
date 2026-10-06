"""archive.today (archive.ph / archive.is / archive.li ...).

archive.today has no official API. Its public web form works like this:
  GET  https://archive.ph/            -> form with a hidden `submitid` token
  POST https://archive.ph/submit/     submitid=...&url=<target>
       -> redirect (Location / Refresh header) to https://archive.ph/<id>
          or to https://archive.ph/wip/<id> while the capture is still running

The service regularly answers automated clients with HTTP 429 and a CAPTCHA page.
This integration does not try to get past that: the submission is marked
"blocked", the service is paused for a while, and the item lands in the review
list with a link the user can open to submit it by hand (and then paste the
resulting archive link back).
"""
import re
from urllib.parse import quote

import httpx

from .base import ArchiveService, ServiceResult

CAPTCHA = re.compile(r"g-recaptcha|h-captcha|hcaptcha|cf-challenge|challenge-platform|turnstile|captcha", re.I)
SUBMIT_ID = re.compile(r"""name=["']submitid["']\s+value=["']([^"']+)["']""")
REFRESH_URL = re.compile(r"url=(\S+)", re.I)
ARCHIVE_ID = re.compile(r"/([A-Za-z0-9]{4,8})/?$")


class ArchiveToday(ArchiveService):
    name = "archive_today"
    label = "archive.today"
    description = "archive.today public submission form"

    def _host(self):
        """Base URL of the mirror; a bare host name means https."""
        host = self.engine.settings.get("archive_today_host", "archive.ph").strip().rstrip("/") or "archive.ph"
        return host if "://" in host else f"https://{host}"

    def configured(self, settings):
        return True, "public form; CAPTCHA pages are left to the user"

    @staticmethod
    def browse_url(url):
        return f"https://archive.ph/{url}"

    def manual_url(self, url):
        return f"{self._host()}/?run=1&url={quote(url, safe='')}"

    def _blocked(self, url, status, why):
        return ServiceResult("blocked", http_status=status, manual_url=self.manual_url(url),
                             error=f"{why} - open the link to submit manually", cooldown=1800)

    async def submit(self, sub) -> ServiceResult:
        client: httpx.AsyncClient = self.engine.client
        host = self._host()
        target = sub["url"]
        try:
            home = await client.get(f"{host}/", timeout=30)
        except httpx.HTTPError as e:
            return ServiceResult("retry", error=f"{host} unreachable: {type(e).__name__}")
        if home.status_code == 429 or CAPTCHA.search(home.text):
            return self._blocked(target, home.status_code, "archive.today asked for a CAPTCHA")
        if home.status_code >= 500:
            return ServiceResult("retry", http_status=home.status_code, error=f"{host} returned HTTP {home.status_code}")

        m = SUBMIT_ID.search(home.text)
        form = {"url": target, "anyway": "1"}
        if m:
            form["submitid"] = m.group(1)
        try:
            r = await client.post(f"{host}/submit/", data=form, timeout=90, follow_redirects=False,
                                  headers={"Referer": f"{host}/"})
        except httpx.HTTPError as e:
            return ServiceResult("retry", error=f"submit failed: {type(e).__name__}")

        location = r.headers.get("location")
        if not location and r.headers.get("refresh"):
            mm = REFRESH_URL.search(r.headers["refresh"])
            location = mm.group(1) if mm else None
        if location:
            if location.startswith("/"):
                location = f"{host}{location}"
            wip = "/wip/" in location
            final = location.replace("/wip/", "/")
            mm = ARCHIVE_ID.search(final)
            return ServiceResult("success", archive_url=final, archive_id=mm.group(1) if mm else None,
                                 http_status=r.status_code,
                                 note="capture was still processing when recorded" if wip else None)
        if r.status_code == 429 or CAPTCHA.search(r.text):
            return self._blocked(target, r.status_code, "archive.today asked for a CAPTCHA")
        if r.status_code >= 500:
            return ServiceResult("retry", http_status=r.status_code, error=f"HTTP {r.status_code} from {host}")
        return ServiceResult("retry", http_status=r.status_code,
                             error=f"unexpected answer from {host} (HTTP {r.status_code}, no archive link)")
