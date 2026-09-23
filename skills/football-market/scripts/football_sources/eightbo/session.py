from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


class EightBOSession:
    """Persistent Scrapling browser session with one safe traffic-gate click."""

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
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.gate_wait_ms = gate_wait_ms
        self._session_kwargs: dict[str, Any] = {
            "headless": headless,
            "real_chrome": real_chrome,
            "user_data_dir": str(self.profile_dir),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "solve_cloudflare": False,
            "disable_resources": False,
            "max_pages": 1,
        }
        if proxy:
            self._session_kwargs["proxy"] = proxy
        self._session: Any = None

    def __enter__(self) -> "EightBOSession":
        try:
            from scrapling.fetchers import StealthySession
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "8BO acquisition needs optional Scrapling dependencies. "
                "Install with: pip install 'scrapling[fetchers]'"
            ) from exc
        self._session = StealthySession(**self._session_kwargs)
        self._session.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session is not None:
            self._session.__exit__(exc_type, exc, tb)
            self._session = None

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

    def fetch(self, url: str) -> Any:
        if self._session is None:
            raise RuntimeError("EightBOSession must be used as a context manager")
        return self._session.fetch(
            url,
            timeout=self.timeout_ms,
            page_action=lambda page: self._page_action(
                page,
                settle_ms=self.settle_ms,
                gate_wait_ms=self.gate_wait_ms,
            ),
        )

