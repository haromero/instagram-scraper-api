"""Limitadores de tasa intercambiables: memoria local o Redis compartido.

``InMemoryRateLimiter`` conserva el comportamiento original (un diccionario por
proceso) y sigue siendo el default. ``RedisRateLimiter`` usa una ventana fija
atómica en Redis para que varios workers sumen el mismo contador.

La interfaz ``RateLimiter`` devuelve siempre el mismo resultado, de modo que
``RateLimitMiddleware`` no necesita saber dónde están los contadores.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from .config import Settings, get_settings
from .redis_backend import build_redis_client

logger = logging.getLogger(__name__)

_MAX_KEYS = 5_000

# Ventana deslizante atómica (ZSET): igual semántica que el backend memoria,
# sin duplicar ráfagas en el borde del bucket de reloj.
# ARGV: now_ms, window_ms, member único, límite.
_SLIDING_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
local limit = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local current = redis.call('ZCARD', KEYS[1])
local allowed = 0
if current < limit then
  redis.call('ZADD', KEYS[1], now, member)
  current = current + 1
  allowed = 1
  redis.call('PEXPIRE', KEYS[1], window)
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local oldest_score = tonumber(oldest[2] or now)
local ttl = redis.call('PTTL', KEYS[1])
return {allowed, current, oldest_score, ttl}
"""


@dataclass(frozen=True)
class RateLimitResult:
    """Resultado de una comprobación de límite."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: int
    retry_after: int
    degraded: bool = False


class RateLimiter(Protocol):
    """Interfaz común de los backends de rate limit."""

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        *,
        critical: bool = False,
    ) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """Ventana fija en memoria del proceso (comportamiento original).

    ``store`` permite compartir el diccionario entre instancias en los tests,
    simulando varios workers con un único almacén.
    """

    def __init__(
        self,
        store: dict[str, list[float]] | None = None,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        self._store = store if store is not None else OrderedDict()
        self._max_keys = max(1, int(max_keys))
        self._lock = threading.Lock()

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        *,
        critical: bool = False,
    ) -> RateLimitResult:
        now = time.time()
        limit = max(1, int(limit))
        with self._lock:
            if len(self._store) >= self._max_keys:
                self._purge(now, window_seconds)
                # Evicción LRU real: purgar solo expirados no limita el tamaño
                # si llegan muchas claves nuevas dentro de la misma ventana.
                while len(self._store) >= self._max_keys:
                    self._store.pop(next(iter(self._store)), None)

            bucket = self._store.setdefault(key, [])
            bucket[:] = [stamp for stamp in bucket if stamp > now - window_seconds]

            if len(bucket) >= limit:
                retry_after = max(
                    1, int(window_seconds - (now - bucket[0])) + 1
                )
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_at=int(now) + retry_after,
                    retry_after=retry_after,
                )

            bucket.append(now)
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=max(0, limit - len(bucket)),
                reset_at=int(now + window_seconds),
                retry_after=0,
            )

    def _purge(self, now: float, window_seconds: float) -> None:
        stale = [
            key
            for key, stamps in self._store.items()
            if not stamps or stamps[-1] <= now - window_seconds
        ]
        for key in stale:
            self._store.pop(key, None)


class RedisRateLimiter:
    """Ventana deslizante atómica compartida por todos los workers.

    ``critical=True`` (petición de autenticación) invierte el fail-open: si
    Redis no responde se rechaza la petición antes que permitir fuerza bruta sin
    control.
    """

    def __init__(self, client, *, fail_open: bool = True) -> None:
        self._client = client
        self._fail_open = fail_open

    def hit(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        *,
        critical: bool = False,
    ) -> RateLimitResult:
        now = time.time()
        limit = max(1, int(limit))
        window_ms = max(1, int(window_seconds * 1000))
        now_ms = int(now * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex}"
        # El middleware pasa "cliente:tipo_de_cubo". El hash tag es solo el
        # cliente para que general/auth/jobs caigan en el mismo slot de Cluster.
        client, separator, bucket = key.rpartition(":")
        if not separator:
            client, bucket = key, "default"
        redis_key = f"rl:{{{client}}}:{bucket}"

        try:
            raw = self._client.eval(
                _SLIDING_SCRIPT, 1, redis_key, now_ms, window_ms, member, limit
            )
        except Exception as exc:  # redis caído, timeout, pool agotado...
            logger.warning("Rate limit Redis no disponible (%s)", exc)
            if critical or not self._fail_open:
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_at=int(now) + 60,
                    retry_after=60,
                    degraded=True,
                )
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=limit,
                reset_at=int(now + window_seconds),
                retry_after=0,
                degraded=True,
            )

        allowed = bool(int(raw[0]))
        current = int(raw[1])
        oldest_score = float(raw[2])
        if allowed:
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=max(0, limit - current),
                reset_at=int(now + window_seconds),
                retry_after=0,
            )

        # El reset real es cuando la entrada más antigua sale de la ventana,
        # no cuando expira la clave completa (que puede contener otras).
        retry_after = max(1, math.ceil((oldest_score + window_ms - now_ms) / 1000))
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_at=int(now + retry_after),
            retry_after=retry_after,
        )


def build_rate_limiter(
    settings: Settings | None = None,
    client=None,
) -> RateLimiter:
    """Construye el limiter indicado por ``IG_RATE_LIMIT_BACKEND``."""
    settings = settings or get_settings()
    if settings.rate_limit_backend == "redis":
        return RedisRateLimiter(
            client if client is not None else build_redis_client(settings),
            fail_open=settings.rate_limit_fail_open,
        )
    return InMemoryRateLimiter()
