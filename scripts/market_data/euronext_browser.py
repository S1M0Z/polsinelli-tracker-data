#!/usr/bin/env python3
"""Browser-backed Euronext provider for JavaScript-injected quotes."""
from __future__ import annotations

import atexit
import os
from typing import Any

from .base import ProviderConfigurationError, ProviderError
from .euronext_page import EuronextPageProvider


class PlaywrightRequester:
    """Render pages with Chromium and return the post-JavaScript DOM."""

    def __init__(
        self,
        *,
        timeout_ms: int = 35_000,
        settle_ms: int = 2_500,
        headless: bool = True,
    ) -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderConfigurationError(
                "Playwright is not installed. Run: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        self._playwright_error = PlaywrightError
        self._timeout_error = PlaywrightTimeoutError
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self._closed = False
        self._playwright = sync_playwright().start()
        launch_args = ["--disable-dev-shm-usage"]
        if os.name != "nt":
            launch_args.append("--no-sandbox")
        self._browser = self._playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )
        self._context = self._browser.new_context(
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36 polsinelli-tracker/3.0"
            ),
        )
        # Quotes are injected through scripts/XHR. Images, video and fonts add
        # substantial bandwidth and memory use without contributing any data.
        self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )
        self._page = self._context.new_page()
        atexit.register(self.close)

    def _accept_cookies(self) -> None:
        labels = (
            "Accept all",
            "Accept All",
            "Tout accepter",
            "Accepter tout",
            "Accetta tutti",
            "Alle akzeptieren",
        )
        for label in labels:
            try:
                locator = self._page.get_by_text(label, exact=True)
                if locator.count():
                    locator.first.click(timeout=800)
                    return
            except (self._playwright_error, self._timeout_error):
                continue

    def get(self, url: str, *, referer: str | None = None) -> str:
        if self._closed:
            raise ProviderError("Euronext browser session is closed")
        try:
            headers: dict[str, str] = {}
            if referer:
                headers["Referer"] = referer
            self._page.set_extra_http_headers(headers)
            response = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            if response is not None and response.status >= 400:
                raise ProviderError(f"Browser HTTP {response.status} for {url}")
            self._accept_cookies()
            try:
                self._page.wait_for_load_state("networkidle", timeout=8_000)
            except self._timeout_error:
                pass
            self._page.wait_for_timeout(self.settle_ms)
            return self._page.content()
        except ProviderError:
            raise
        except (self._playwright_error, self._timeout_error) as exc:
            raise ProviderError(f"Browser rendering failed for {url}: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in (self._context, self._browser, self._playwright):
            try:
                resource.close() if hasattr(resource, "close") else resource.stop()
            except Exception:
                pass


class EuronextBrowserProvider(EuronextPageProvider):
    """Euronext provider that sees the same rendered quote DOM as a user browser."""

    def __init__(self, **kwargs: Any) -> None:
        requester = kwargs.pop("requester", None)
        self.browser_requester = requester or PlaywrightRequester()
        super().__init__(requester=self.browser_requester.get, **kwargs)

    def close(self) -> None:
        close = getattr(self.browser_requester, "close", None)
        if callable(close):
            close()
