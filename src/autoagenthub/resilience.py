"""Resilience primitives: Circuit Breaker, Token-Bucket Rate Limiter, and Exponential Backoff."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the circuit breaker is open."""


@dataclass
class CircuitBreaker:
    """Async-safe circuit breaker to isolate failing external providers."""

    name: str
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    opened_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def allow_request(self) -> bool:
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if (time.monotonic() - self.opened_at) >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    return True
                return False
            # HALF_OPEN allows a single probe
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self.failure_count = 0
            self.state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self.failure_count += 1
            if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if not await self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN")
        try:
            result = await fn()
        except Exception:
            await self.record_failure()
            raise
        await self.record_success()
        return result


@dataclass
class TokenBucketRateLimiter:
    """Async token-bucket rate limiter for upstream API protection."""

    rate_per_minute: int = 30
    capacity: int = 30
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                refill = elapsed * (self.rate_per_minute / 60.0)
                if refill > 0:
                    self._tokens = min(float(self.capacity), self._tokens + refill)
                    self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / (self.rate_per_minute / 60.0)
            await asyncio.sleep(min(wait_seconds, 1.0))


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
) -> T:
    """Execute an async callable with exponential backoff and full jitter."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except CircuitOpenError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            jittered = random.uniform(0.5 * delay, delay)
            await asyncio.sleep(jittered)
    assert last_exc is not None
    raise last_exc
