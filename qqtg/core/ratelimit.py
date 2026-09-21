"""Token-bucket rate limiters (global + per chat) used by the adapters."""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = max(0.01, rate_per_sec)
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    def reconfigure(self, rate_per_sec: float, burst: float | None = None) -> None:
        self.rate = max(0.01, rate_per_sec)
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self.tokens = min(self.tokens, self.capacity)

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                wait = (tokens - self.tokens) / self.rate
                await asyncio.sleep(wait)


class ChatRateLimiter:
    """Global bucket + one bucket per chat, plus per-chat 'retry after' holds
    (e.g. Telegram 429)."""

    def __init__(self, global_per_sec: float, chat_per_sec: float, chat_burst: float | None = None):
        self.global_bucket = TokenBucket(global_per_sec)
        self.chat_per_sec = chat_per_sec
        self.chat_burst = chat_burst
        self._chats: dict[str, TokenBucket] = {}
        self._holds: dict[str, float] = {}

    def reconfigure(self, global_per_sec: float, chat_per_sec: float, chat_burst: float | None = None) -> None:
        self.global_bucket.reconfigure(global_per_sec)
        self.chat_per_sec = chat_per_sec
        self.chat_burst = chat_burst
        for b in self._chats.values():
            b.reconfigure(chat_per_sec, chat_burst)

    def _bucket(self, chat_id: str) -> TokenBucket:
        b = self._chats.get(chat_id)
        if b is None:
            b = TokenBucket(self.chat_per_sec, self.chat_burst)
            self._chats[chat_id] = b
        return b

    def hold(self, chat_id: str, seconds: float) -> None:
        self._holds[chat_id] = max(self._holds.get(chat_id, 0), time.monotonic() + seconds)

    async def acquire(self, chat_id: str) -> None:
        until = self._holds.get(chat_id)
        if until:
            delay = until - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._holds.pop(chat_id, None)
        await self._bucket(chat_id).acquire()
        await self.global_bucket.acquire()
