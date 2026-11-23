"""Revocación de tokens por ``jti`` con almacén intercambiable.

Con el store en memoria la revocación solo afecta al worker que la registró.
Con Redis (``IG_RATE_LIMIT_BACKEND=redis``) es global: cualquier worker que
valide el JWT ve la revocación. Las funciones ``revoke`` e ``is_revoked`` se
mantienen por compatibilidad con ``security.py`` y los tests.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from .config import Settings, get_settings
from .redis_backend import build_redis_client

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rev:"


class RevocationStore(Protocol):
    """Interfaz común de los almacenes de revocación."""

    def revoke(self, jti: str, expires_at: float) -> None: ...

    def is_revoked(self, jti: str) -> bool: ...


class InMemoryRevocationStore:
    """Almacén local con purga por expiración (comportamiento original)."""

    def __init__(self) -> None:
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    def revoke(self, jti: str, expires_at: float) -> None:
        now = time.time()
        with self._lock:
            self._purge(now)
            if expires_at > now:
                self._revoked[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        now = time.time()
        with self._lock:
            self._purge(now)
            return jti in self._revoked

    def _purge(self, now: float) -> None:
        expired = [
            key for key, expires_at in self._revoked.items() if expires_at <= now
        ]
        for key in expired:
            self._revoked.pop(key, None)


class RedisRevocationStore:
    """Revocación compartida con ``SET`` + TTL hasta que expire el JWT.

    Si Redis no responde durante ``revoke``, la revocación queda en una lista
    local pendiente y se reintenta en la siguiente comprobación. Así un corte
    no "resucita" tokens ni pierde revocaciones recientes al recuperarse.
    """

    def __init__(self, client, *, fail_open: bool = False) -> None:
        self._client = client
        self._fail_open = fail_open
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def _flush(self) -> None:
        with self._lock:
            pending = list(self._pending.items())
        expired = [jti for jti, expires_at in pending if expires_at <= time.time()]
        for jti in expired:
            with self._lock:
                self._pending.pop(jti, None)
        for jti, expires_at in pending:
            if expires_at <= time.time():
                continue
            ttl = int(expires_at - time.time()) + 1
            if ttl <= 0:
                continue
            try:
                self._client.set(f"{_KEY_PREFIX}{jti}", "1", ex=ttl)
            except Exception:
                return
            with self._lock:
                self._pending.pop(jti, None)

    def revoke(self, jti: str, expires_at: float) -> None:
        ttl = int(expires_at - time.time()) + 1
        if ttl <= 0:
            return
        with self._lock:
            self._pending[jti] = expires_at
        self._flush()

    def is_revoked(self, jti: str) -> bool:
        self._flush()
        try:
            if self._client.exists(f"{_KEY_PREFIX}{jti}"):
                return True
        except Exception as exc:
            # fail-open: no tumbar la autenticación si Redis cae; fail-closed:
            # rechazar tokens porque no podemos comprobar su revocación.
            logger.warning("No se pudo comprobar la revocación en Redis (%s)", exc)
            if not self._fail_open:
                return True
        with self._lock:
            return jti in self._pending


def build_revocation_store(
    settings: Settings | None = None,
    client=None,
) -> RevocationStore:
    """Construye el store indicado por ``IG_RATE_LIMIT_BACKEND``."""
    settings = settings or get_settings()
    if settings.rate_limit_backend == "redis":
        return RedisRevocationStore(
            client if client is not None else build_redis_client(settings),
            fail_open=settings.revocation_fail_open,
        )
    return InMemoryRevocationStore()


_store: RevocationStore = InMemoryRevocationStore()


def configure(store: RevocationStore) -> None:
    """Sustituye el store global (lo hace ``app.main`` al arrancar)."""
    global _store
    _store = store


def revoke(jti: str, expires_at: float) -> None:
    """Marca un token como revocado hasta que expire."""
    _store.revoke(jti, expires_at)


def is_revoked(jti: str) -> bool:
    """Indica si el token está revocado en el store configurado."""
    return _store.is_revoked(jti)
