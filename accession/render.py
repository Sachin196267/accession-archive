"""Optional browser rendering for JavaScript-built pages (Playwright / Chromium).

Rendering is used in two places:
  * discovery - links that only exist after scripts run are added to the inventory;
  * the local snapshot service - the stored copy is the rendered DOM.

Pages that redirect to a login screen are reported as such; nothing here signs in,
solves CAPTCHAs or otherwise gets around access controls.
"""
import asyncio
import importlib.util
import re
import time
from dataclasses import dataclass

LOGIN_WALL = re.compile(r"/(accounts/)?log[-_]?in|/signin|/auth/|challenge", re.I)


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


@dataclass
class Rendered:
    url: str
    final_url: str | None = None
    status: int | None = None
    html: str = ""
    elapsed_ms: int = 0
    error: str | None = None
    login_wall: bool = False


class Renderer:
    """One shared headless browser; each render gets its own context."""

    def __init__(self, user_agent: str, max_pages: int = 2):
        self.user_agent = user_agent
        self._sem = asyncio.Semaphore(max_pages)
        self._pw = self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)

    async def render(self, url: str, timeout_ms: int = 30000) -> Rendered:
        out = Rendered(url=url)
        t0 = time.perf_counter()
        try:
            await self._ensure()
            async with self._sem:
                ctx = await self._browser.new_context(user_agent=self.user_agent)
                try:
                    page = await ctx.new_page()
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass  # long-polling pages never go idle; the DOM is usable anyway
                    out.status = resp.status if resp else None
                    out.final_url = page.url
                    out.html = await page.content()
                    out.login_wall = bool(LOGIN_WALL.search(page.url)) and not LOGIN_WALL.search(url)
                finally:
                    await ctx.close()
        except Exception as e:  # browser missing, navigation timeout, ...
            out.error = f"render failed: {str(e).splitlines()[0][:160]}"
        out.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return out

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None
