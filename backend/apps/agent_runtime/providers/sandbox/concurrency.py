"""Company-scoped Redis leases for limiting concurrent cloud sandboxes."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict

from django.conf import settings

from .errors import SandboxCreateError


logger = logging.getLogger("agent_runtime.sandbox")


class SandboxConcurrencyLimitExceeded(SandboxCreateError):
    """Raised before cloud creation when a company has no free slot."""

    def __init__(self, message: str, *, scope: str = "company") -> None:
        super().__init__(message)
        self.scope = scope


class SandboxConcurrencyUnavailable(SandboxCreateError):
    """Raised when Redis cannot enforce the cost and concurrency boundary."""


class RedisCompanySandboxLimiter:
    """Atomic company and platform semaphore backed by Redis sorted sets.

    The two limits are acquired in one Lua operation, so a rejected request
    cannot consume only one of the slots. A Run ID is the company member and a
    company-scoped Run ID is the global member, keeping retries idempotent.
    """

    _ACQUIRE_SCRIPT = """
local company_key = KEYS[1]
local global_key = KEYS[2]
local now_ms = tonumber(ARGV[1])
local expires_ms = tonumber(ARGV[2])
local company_limit = tonumber(ARGV[3])
local global_limit = tonumber(ARGV[4])
local company_member = ARGV[5]
local global_member = ARGV[6]
redis.call('ZREMRANGEBYSCORE', company_key, '-inf', now_ms)
redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now_ms)
local company_exists = redis.call('ZSCORE', company_key, company_member)
local global_exists = redis.call('ZSCORE', global_key, global_member)
if not company_exists and redis.call('ZCARD', company_key) >= company_limit then
  return -1
end
if not global_exists and redis.call('ZCARD', global_key) >= global_limit then
  return -2
end
local lease_ms = math.max(expires_ms - now_ms, 1000)
redis.call('ZADD', company_key, expires_ms, company_member)
redis.call('ZADD', global_key, expires_ms, global_member)
redis.call('PEXPIRE', company_key, lease_ms)
redis.call('PEXPIRE', global_key, lease_ms)
return 1
"""

    _RELEASE_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[2])
return 1
"""

    def __init__(self, connection=None, *, prefix: str | None = None) -> None:
        self._connection = connection
        self.prefix = prefix or getattr(
            settings,
            "AGENT_RUNTIME_SANDBOX_REDIS_PREFIX",
            "touming:agent-runtime:sandbox-leases",
        )

    def acquire(self, company_id: object, run_id: str, *, limit: int, ttl_seconds: int) -> None:
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + max(int(ttl_seconds), 1) * 1000
        global_limit = max(
            int(getattr(settings, "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES", 25)),
            1,
        )
        try:
            accepted = self.connection.eval(
                self._ACQUIRE_SCRIPT,
                2,
                self._company_key(company_id),
                self._global_key(),
                now_ms,
                expires_ms,
                max(int(limit), 1),
                global_limit,
                str(run_id),
                self._global_member(company_id, run_id),
            )
        except Exception as exc:
            if not bool(
                getattr(settings, "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR", True)
            ):
                logger.exception(
                    "sandbox concurrency control unavailable; fail-open is enabled"
                )
                return
            raise SandboxConcurrencyUnavailable("sandbox concurrency control is unavailable") from exc
        if int(accepted or 0) == -1:
            raise SandboxConcurrencyLimitExceeded(
                "company sandbox concurrency limit exceeded",
                scope="company",
            )
        if int(accepted or 0) == -2:
            raise SandboxConcurrencyLimitExceeded(
                "global sandbox concurrency limit exceeded",
                scope="global",
            )
        if int(accepted or 0) != 1:
            raise SandboxConcurrencyUnavailable("sandbox concurrency control returned an invalid result")

    def release(self, company_id: object, run_id: str) -> None:
        try:
            self.connection.eval(
                self._RELEASE_SCRIPT,
                2,
                self._company_key(company_id),
                self._global_key(),
                str(run_id),
                self._global_member(company_id, run_id),
            )
        except Exception as exc:
            if not bool(
                getattr(settings, "AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR", True)
            ):
                logger.exception(
                    "sandbox concurrency release unavailable; fail-open is enabled"
                )
                return
            raise SandboxConcurrencyUnavailable("sandbox concurrency release failed") from exc

    @property
    def connection(self):
        if self._connection is None:
            from django_redis import get_redis_connection

            self._connection = get_redis_connection("default")
        return self._connection

    def _company_key(self, company_id: object) -> str:
        return f"{self.prefix}:company:{self._company_digest(company_id)}"

    def _global_key(self) -> str:
        return f"{self.prefix}:global"

    def _global_member(self, company_id: object, run_id: str) -> str:
        return f"{self._company_digest(company_id)}:{run_id}"

    @staticmethod
    def _company_digest(company_id: object) -> str:
        digest = hashlib.sha256(str(company_id).encode("utf-8")).hexdigest()[:24]
        return digest


class InMemoryCompanySandboxLimiter:
    """Deterministic, thread-safe limiter used by unit tests."""

    def __init__(self, *, global_limit: int | None = None) -> None:
        self._leases: dict[str, dict[str, float]] = defaultdict(dict)
        self._global_leases: dict[str, float] = {}
        self.global_limit = global_limit
        self._lock = threading.Lock()

    def acquire(self, company_id: object, run_id: str, *, limit: int, ttl_seconds: int) -> None:
        now = time.time()
        key = str(company_id)
        global_key = f"{key}:{run_id}"
        with self._lock:
            active = self._leases[key]
            self._discard_expired(active, now)
            self._discard_expired(self._global_leases, now)
            if str(run_id) not in active and len(active) >= max(int(limit), 1):
                raise SandboxConcurrencyLimitExceeded(
                    "company sandbox concurrency limit exceeded",
                    scope="company",
                )
            global_limit = max(
                int(
                    self.global_limit
                    if self.global_limit is not None
                    else getattr(settings, "AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES", 25)
                ),
                1,
            )
            if global_key not in self._global_leases and len(self._global_leases) >= global_limit:
                raise SandboxConcurrencyLimitExceeded(
                    "global sandbox concurrency limit exceeded",
                    scope="global",
                )
            expires_at = now + max(int(ttl_seconds), 1)
            active[str(run_id)] = expires_at
            self._global_leases[global_key] = expires_at

    def release(self, company_id: object, run_id: str) -> None:
        with self._lock:
            self._leases[str(company_id)].pop(str(run_id), None)
            self._global_leases.pop(f"{company_id}:{run_id}", None)

    def active_count(self, company_id: object) -> int:
        with self._lock:
            return len(self._leases[str(company_id)])

    def global_active_count(self) -> int:
        with self._lock:
            return len(self._global_leases)

    @staticmethod
    def _discard_expired(leases: dict[str, float], now: float) -> None:
        for member, expires_at in tuple(leases.items()):
            if expires_at <= now:
                leases.pop(member, None)
