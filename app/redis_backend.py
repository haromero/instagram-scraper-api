"""Cliente Redis compartido por el rate limit, la revocación y los candados.

El módulo importa ``redis`` de forma diferida para que el backend ``memory``
siga funcionando sin la dependencia instalada. Un único cliente sirve a los
tres componentes y se cierra al apagar la aplicación.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:  # pragma: no cover - solo para type checkers
    from redis import Redis


# Cliente único de la aplicación. Se registra en el arranque para que health
# pueda comprobar Redis sin importar ``main`` (evita imports circulares).
_client: "Redis | None" = None


def set_client(client: "Redis | None") -> None:
    global _client
    _client = client


def get_client() -> "Redis | None":
    return _client


def build_redis_client(settings: Settings) -> "Redis | None":
    """Devuelve un cliente Redis o ``None`` si el backend es ``memory``."""
    if settings.rate_limit_backend != "redis":
        return None

    import redis  # importación diferida: memory no necesita el paquete

    return redis.Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        decode_responses=True,
    )
