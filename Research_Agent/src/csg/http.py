"""Cached, rate-limited async HTTP.

Every response is cached to data/cache/ keyed by sha256(url), so re-runs and
scoring iterations cost zero network traffic. Per-host locks enforce a
politeness delay; concurrency is capped globally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "data" / "cache"


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        per_host_delay: float = 1.0,
        timeout: float = 20.0,
        cache_ttl_hours: float = 168.0,
        max_concurrency: int = 8,
        use_cache: bool = True,
    ) -> None:
        self.cache_ttl = cache_ttl_hours * 3600
        self.per_host_delay = per_host_delay
        self.use_cache = use_cache
        self._sem = asyncio.Semaphore(max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    # -- cache ------------------------------------------------------------
    def _cache_key(self, url: str, body: Optional[str] = None) -> str:
        raw = url if body is None else f"{url}\n{body}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return CACHE_DIR / (key + ".json")

    def _read_cache(self, key: str) -> Optional[dict[str, Any]]:
        if not self.use_cache:
            return None
        p = self._cache_path(key)
        if not p.exists() or (time.time() - p.stat().st_mtime) > self.cache_ttl:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, key: str, url: str, status: int, body: str) -> None:
        try:
            self._cache_path(key).write_text(
                json.dumps({"url": url, "status": status, "body": body}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # -- fetch ------------------------------------------------------------
    async def get(self, url: str) -> tuple[int, str]:
        """Return (status, body). Network failures surface as status 0."""
        key = self._cache_key(url)
        cached = self._read_cache(key)
        if cached is not None:
            self.stats["hits"] += 1
            return cached["status"], cached["body"]

        host = urlparse(url).netloc
        lock = self._host_locks.setdefault(host, asyncio.Lock())

        async with self._sem:
            async with lock:
                gap = time.monotonic() - self._host_last.get(host, 0.0)
                if gap < self.per_host_delay:
                    await asyncio.sleep(self.per_host_delay - gap)
                try:
                    r = await self._client.get(url)
                    status, body = r.status_code, r.text
                except (httpx.HTTPError, UnicodeDecodeError):
                    self.stats["errors"] += 1
                    status, body = 0, ""
                finally:
                    self._host_last[host] = time.monotonic()

        self.stats["misses"] += 1
        # Cache negatives too -- a 404 board token should not be re-probed.
        if status in (0, 200, 404, 403, 410):
            self._write_cache(key, url, status, body)
        return status, body

    async def get_json(self, url: str) -> tuple[int, Any]:
        status, body = await self.get(url)
        if status != 200 or not body:
            return status, None
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, None

    async def post_json(self, url: str, json_body: dict[str, Any]) -> tuple[int, Any]:
        """POST with a JSON body, cached by (url, body) -- for APIs like Workday's
        CXS job list that require POST instead of GET. Same caching/politeness
        contract as `get`, just keyed on the request body too since a POST's
        response depends on it."""
        body_str = json.dumps(json_body, sort_keys=True)
        key = self._cache_key(url, body_str)
        cached = self._read_cache(key)
        if cached is not None:
            self.stats["hits"] += 1
            status, text = cached["status"], cached["body"]
        else:
            host = urlparse(url).netloc
            lock = self._host_locks.setdefault(host, asyncio.Lock())

            async with self._sem:
                async with lock:
                    gap = time.monotonic() - self._host_last.get(host, 0.0)
                    if gap < self.per_host_delay:
                        await asyncio.sleep(self.per_host_delay - gap)
                    try:
                        r = await self._client.post(url, json=json_body)
                        status, text = r.status_code, r.text
                    except (httpx.HTTPError, UnicodeDecodeError):
                        self.stats["errors"] += 1
                        status, text = 0, ""
                    finally:
                        self._host_last[host] = time.monotonic()

            self.stats["misses"] += 1
            if status in (0, 200, 404, 403, 410):
                self._write_cache(key, url, status, text)

        if status != 200 or not text:
            return status, None
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, None
