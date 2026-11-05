"""HTTP 封装（《需求与方案说明》第 3.2 节抓取礼仪）。

可识别的 User-Agent；429 / 5xx / 网络错误退避重试（遵守 Retry-After）；识别 Cloudflare 质询并立即失败，
不做任何绕过。采集在后台线程中运行，使用同步客户端（D25）。
"""

import logging
import time
from collections.abc import Callable

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
CHALLENGE_MARKERS = (b"<title>Just a moment", b"Attention Required! | Cloudflare")
MAX_RETRY_AFTER = 60.0


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{reason}（{url}" + (f"，HTTP {status}）" if status else "）"))
        self.url = url
        self.reason = reason
        self.status = status


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        retries: int = 2,
        backoff: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout,
            follow_redirects=True,
            proxy=settings.https_proxy or None,
            transport=transport,
        )
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, *, timeout: float | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                resp = self._client.get(url, headers=headers, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            except httpx.TransportError as e:
                if last:
                    raise FetchError(url, f"网络错误 {type(e).__name__}") from e
                self._wait(url, attempt, None, type(e).__name__)
                continue
            if self._is_challenge(resp):
                raise FetchError(url, "Cloudflare 质询", resp.status_code)
            if resp.status_code in RETRY_STATUS and not last:
                self._wait(url, attempt, resp, f"HTTP {resp.status_code}")
                continue
            if resp.is_error:
                raise FetchError(url, "HTTP 错误", resp.status_code)
            return resp
        raise AssertionError("unreachable")

    def _wait(self, url: str, attempt: int, resp: httpx.Response | None, why: str) -> None:
        delay = self._backoff * 2**attempt
        if resp is not None:
            # Retry-After（秒）优先；Reddit 等只给 X-RateLimit-Reset（距重置的秒数）
            for header in ("Retry-After", "X-RateLimit-Reset"):
                value = resp.headers.get(header, "").strip()
                try:
                    delay = min(max(float(value), 1.0), MAX_RETRY_AFTER)
                    break
                except ValueError:
                    continue
        log.warning("%s，%.0f 秒后重试（第 %d 次）：%s", why, delay, attempt + 1, url)
        self._sleep(delay)

    @staticmethod
    def _is_challenge(resp: httpx.Response) -> bool:
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        head = resp.content[:20000]
        return any(marker in head for marker in CHALLENGE_MARKERS)
