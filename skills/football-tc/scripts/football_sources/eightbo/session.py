from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


class EightBOSession:
    """Small 8BO request adapter backed by Scrapling's static Fetcher."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = False,
        real_chrome: bool = True,
        timeout_ms: int = 60_000,
        settle_ms: int = 1_800,
        gate_wait_ms: int = 10_000,
        proxy: Optional[str] = None,
    ) -> None:
        self.profile_dir = profile_dir.expanduser().resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = max(1, int(timeout_ms / 1000))
        self._fetcher: Any = None

    def __enter__(self) -> "EightBOSession":
        try:
            from scrapling.fetchers import Fetcher
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "8BO acquisition needs optional Scrapling dependencies. "
                "Install with: pip install 'scrapling[fetchers]'"
            ) from exc
        self._fetcher = Fetcher
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._fetcher = None

    @staticmethod
    def _page_action(page: Any, *, settle_ms: int, gate_wait_ms: int) -> None:
        text = page.locator("body").inner_text()
        if "当前访问量较大" in text:
            button = page.get_by_role("button", name="下一步")
            if button.count() == 1:
                button.click()
                page.wait_for_timeout(gate_wait_ms)
                return
        page.wait_for_timeout(settle_ms)

    def get(self, url: str, **kwargs: Any) -> Any:
        if self._fetcher is None:
            raise RuntimeError("EightBOSession must be used as a context manager")
        kwargs.setdefault("timeout", self.timeout_seconds)
        return self._fetcher.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        if self._fetcher is None:
            raise RuntimeError("EightBOSession must be used as a context manager")
        kwargs.setdefault("timeout", self.timeout_seconds)
        return self._fetcher.post(url, **kwargs)

    def fetch(self, url: str, *, method: str = "GET", **kwargs: Any) -> Any:
        if method.upper() == "POST":
            return self.post(url, **kwargs)
        return self.get(url, **kwargs)
