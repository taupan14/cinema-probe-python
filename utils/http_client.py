"""
utils/http_client.py
Async HTTP client dengan retry, rate limiting, dan rotating headers.
"""
from __future__ import annotations
import asyncio
import random
import logging
from typing import Optional
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def get_random_headers(referer: str = "") -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def get_json_headers(origin: str = "") -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin + "/"
    return headers


class RateLimiter:
    """Token bucket rate limiter untuk async."""

    def __init__(self, rate: float = 1.0):
        self.rate = rate
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_call
            wait_time = max(0, self.rate - elapsed)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._last_call = asyncio.get_event_loop().time()


class AsyncHTTPClient:
    """
    Wrapper httpx.AsyncClient dengan:
    - Automatic retry (exponential backoff)
    - Rate limiting per-instance
    - Header rotation
    - Timeout handling
    """

    def __init__(
        self,
        base_url: str = "",
        delay: float = 1.5,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url
        self.rate_limiter = RateLimiter(rate=delay)
        self.timeout = httpx.Timeout(timeout)
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            verify=False,  # beberapa site punya SSL issue

        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get(self, url: str, headers: Optional[dict] = None, params: Optional[dict] = None) -> httpx.Response:
        await self.rate_limiter.acquire()
        _headers = headers or get_random_headers(self.base_url)

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.get(url, headers=_headers, params=params)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
                if attempt == self.max_retries:
                    logger.error(f"[{attempt}/{self.max_retries}] FAILED {url}: {e}")
                    raise
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"[{attempt}/{self.max_retries}] Retry in {wait:.1f}s → {url}: {e}")
                await asyncio.sleep(wait)

    async def post(self, url: str, json: dict = None, headers: Optional[dict] = None) -> httpx.Response:
        await self.rate_limiter.acquire()
        _headers = headers or get_json_headers(self.base_url)

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(url, json=json, headers=_headers)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
                if attempt == self.max_retries:
                    logger.error(f"[{attempt}/{self.max_retries}] POST FAILED {url}: {e}")
                    raise
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning(f"[{attempt}/{self.max_retries}] Retry in {wait:.1f}s → {url}: {e}")
                await asyncio.sleep(wait)
