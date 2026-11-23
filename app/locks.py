"""Candados intercambiables para operaciones atómicas entre workers.

La comprobación de cuota de jobs y la creación del job deben ocurrir como una
sola operación: si dos workers leen el mismo contador antes de insertar, ambos
creen un job y se supera el límite. Con el backend ``memory`` basta un
``threading.Lock``; con Redis se usa un candado distribuido ``SET NX`` con un
TTL y una liberación verificada por Lua (solo borra la clave si sigue siendo
el dueño, para no borrar el candado de otro worker tras una expiración).

La interfaz ``DistributedLock`` expone un context manager ``hold``; el resto
de la aplicación no necesita saber qué backend hay detrás.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Protocol

from .config import Settings, get_settings
from .errors import APIError
from .redis_backend import build_redis_client, get_client

if TYPE_CHECKING:  # pragma: no cover - solo para type checkers
    from redis import Redis

logger = logging.getLogger(__name__)

# Borra la clave solo si el valor sigue siendo nuestro token.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


class DistributedLock(Protocol):
    """Interfaz común: ``with lock.hold(name):`` protege la sección crítica."""

    @contextmanager
    def hold(self, name: str) -> Iterator[None]: ...


class LocalLock:
    """Candado de un solo proceso, independiente por nombre (owner)."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            yield


class RedisLock:
    """Candado distribuido por nombre, con degradación configurable.

    Si Redis no responde y ``fail_open`` es ``True`` se usa el candado local:
    se pierde la atomicidad entre workers durante el corte, pero el servicio
    sigue aceptando jobs. Con ``fail_open=False`` se rechaza la petición.
    """

    def __init__(
        self,
        client: "Redis",
        *,
        fail_open: bool = True,
        ttl_seconds: float = 10.0,
        timeout_seconds: float = 2.0,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self._client = client
        self._fail_open = fail_open
        self._ttl_ms = max(100, int(ttl_seconds * 1000))
        self._timeout_seconds = timeout_seconds
        # Renovar a un tercio del TTL evita que una sección crítica lenta deje
        # entrar a otro worker mientras el primero sigue trabajando.
        self._heartbeat_seconds = max(
            0.01, float(heartbeat_seconds or ttl_seconds / 3)
        )
        self._fallback = LocalLock()

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        key = f"lock:{name}"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._timeout_seconds

        while True:
            try:
                acquired = bool(
                    self._client.set(key, token, nx=True, px=self._ttl_ms)
                )
            except Exception as exc:
                logger.warning("Candado Redis no disponible (%s)", exc)
                if not self._fail_open:
                    raise APIError(
                        503,
                        "SERVICE_UNAVAILABLE",
                        "El servicio de cuotas no está disponible.",
                        {"lock": "down"},
                        headers={"Retry-After": "5"},
                    )
                with self._fallback.hold(name):
                    yield
                return

            if acquired:
                break
            if time.monotonic() >= deadline:
                # Otro worker tiene el candado; pedirle al cliente que reintente
                # en lugar de crear un job por encima de la cuota.
                raise APIError(
                    429,
                    "CONCURRENCY_LIMIT",
                    "Demasiadas peticiones simultáneas; reintenta.",
                    {"retry_after_seconds": 1},
                    headers={"Retry-After": "1"},
                )
            time.sleep(0.02)

        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(self._heartbeat_seconds):
                try:
                    self._client.pexpire(key, self._ttl_ms)
                except Exception as exc:  # pragma: no cover - best effort
                    logger.warning("No se pudo renovar el candado Redis (%s)", exc)

        heartbeat = threading.Thread(
            target=renew, name="ig-lock-heartbeat", daemon=True
        )
        heartbeat.start()
        try:
            yield
        finally:
            stop.set()
            heartbeat.join(timeout=0.2)
            try:
                self._client.eval(_RELEASE_SCRIPT, 1, key, token)
            except Exception as exc:  # pragma: no cover - logging best-effort
                logger.warning("No se pudo liberar el candado Redis (%s)", exc)


quota_lock: DistributedLock = LocalLock()


def configure(lock: DistributedLock) -> None:
    """Sustituye el candado global (lo hace ``app.main`` al arrancar)."""
    global quota_lock
    quota_lock = lock


def build_quota_lock(
    settings: Settings | None = None,
    client: "Redis | None" = None,
) -> DistributedLock:
    """Construye el candado indicado por ``IG_RATE_LIMIT_BACKEND``."""
    settings = settings or get_settings()
    if settings.rate_limit_backend == "redis":
        return RedisLock(
            client if client is not None else (get_client() or build_redis_client(settings)),
            fail_open=settings.rate_limit_fail_open,
            ttl_seconds=settings.job_lock_ttl_seconds,
            timeout_seconds=settings.job_lock_timeout_seconds,
        )
    return LocalLock()
