"""Render KIT's JavaScript browser verification using a normal Chromium session.

Cookies remain in memory. No browser profile or page content is written to disk.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from .config import PORTAL_BASE


def is_browser_challenge(html: str) -> bool:
    text = html.lower()
    return "verifying your browser" in text and "javascript is required" in text


class BrowserReader:
    def __init__(self, cookiejar: Any) -> None:
        self.cookiejar = cookiejar
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._responses = {}

    async def _start(self) -> None:
        if self._page is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()
        self._context = await self._browser.new_context(locale="de-DE")
        cookies = []
        for cookie in self.cookiejar:
            entry = {
                "name": cookie.name, "value": cookie.value,
                "domain": cookie.domain, "path": cookie.path or "/",
                "secure": cookie.secure,
            }
            if cookie.expires and cookie.expires > time.time():
                entry["expires"] = cookie.expires
            elif cookie.expires:
                continue
            cookies.append(entry)
        await self._context.add_cookies(cookies)
        self._page = await self._context.new_page()
        self._page.on("response", self._record_response)

    def _record_response(self, response: Any) -> None:
        if response.request.is_navigation_request():
            self._responses[response.frame] = response

    async def get(self, url: str) -> tuple[int, str, str]:
        await self._start()
        self._responses.clear()
        target = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(target.query)
                 if k.lower() not in {"login-token", "login-ts"}]
        relative = target.path.lstrip("/") + "?" + urlencode(query)
        # The official portal performs the backend session handoff and retries
        # its iframe after SSO. Direct navigation can lose the requested page.
        wrapper = PORTAL_BASE + "/exams/registration.php#!" + relative
        await self._page.goto(wrapper, wait_until="domcontentloaded", timeout=60000)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for frame in self._page.frames:
                actual = urlparse(frame.url)
                if actual.hostname != target.hostname or actual.path != target.path:
                    continue
                try:
                    await frame.wait_for_load_state("load", timeout=10000)
                    html = await frame.content()
                except Exception:
                    continue  # The portal can replace its iframe after login.
                if is_browser_challenge(html):
                    continue
                response = self._responses.get(frame)
                if response is not None:
                    return response.status, html, frame.url
            await asyncio.sleep(1)
        raise RuntimeError("KIT browser verification or student page loading did not complete.")

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
