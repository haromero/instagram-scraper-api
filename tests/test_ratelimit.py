"""Tests del rate limit distribuido (sin servidor Redis real).

``FakeRedis`` reproduce exactamente la semántica del script Lua de
``RedisRateLimiter``: incrementa el contador, pone la expiración en el primer
acceso y devuelve ``[count, ttl_ms]``. Así se comprueba la lógica del
adaptador (claves, límites, degradación) sin depender de Redis ni de módulos
compilados.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.middleware import RateLimitMiddleware
from app.ratelimit import RateLimitResult
from app.ratelimit import (
    InMemoryRateLimiter,
    RedisRateLimiter,
    build_rate_limiter,
)


class FakeRedis:
    """ZSET mínima que reproduce el script Lua de ventana deslizante."""

    def __init__(self) -> None:
        self.data: dict[str, list[tuple[float, str]]] = {}
        self.keys: list[str] = []
        self.fail = False

    def eval(self, script, numkeys, key, now_ms, window_ms, member, limit):
        assert numkeys == 1
        assert "ZADD" in script and "ZREMRANGEBYSCORE" in script and "PEXPIRE" in script
        self.keys.append(key)
        if self.fail:
            raise RuntimeError("redis caído")

        now, window = float(now_ms), float(window_ms)
        entries = [
            (score, name)
            for score, name in self.data.get(key, [])
            if score > now - window
        ]
        allowed = 0
        if len(entries) < int(limit):
            entries.append((now, str(member)))
            allowed = 1
        self.data[key] = entries
        oldest = min((score for score, _ in entries), default=now)
        return [allowed, len(entries), oldest, window]


# ---------------------------------------------------------------------------
# Backend en memoria
# ---------------------------------------------------------------------------
def test_memory_limiter_enforces_limit_and_window():
    limiter = InMemoryRateLimiter()

    first = limiter.hit("sub:cli", limit=2, window_seconds=60)
    second = limiter.hit("sub:cli", limit=2, window_seconds=60)
    third = limiter.hit("sub:cli", limit=2, window_seconds=60)

    assert first.allowed and second.allowed
    assert (first.remaining, second.remaining) == (1, 0)
    assert not third.allowed
    assert third.remaining == 0
    assert 1 <= third.retry_after <= 61
    assert third.reset_at > first.reset_at - 60


def test_memory_limiter_separates_keys():
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        assert limiter.hit("sub:a", limit=3, window_seconds=60).allowed
    assert limiter.hit("sub:b", limit=1, window_seconds=60).allowed
    assert not limiter.hit("sub:a", limit=3, window_seconds=60).allowed


def test_memory_limiter_ignores_expired_stamps():
    limiter = InMemoryRateLimiter()
    limiter.hit("sub:cli", limit=1, window_seconds=0.05)
    time.sleep(0.06)
    assert limiter.hit("sub:cli", limit=1, window_seconds=0.05).allowed


def test_two_instances_sharing_store_share_the_counter():
    """Simula dos workers: con un almacén común el límite no se multiplica."""
    store: dict[str, list[float]] = {}
    worker_a = InMemoryRateLimiter(store)
    worker_b = InMemoryRateLimiter(store)

    assert worker_a.hit("sub:cli", limit=3, window_seconds=60).allowed
    assert worker_b.hit("sub:cli", limit=3, window_seconds=60).allowed
    assert worker_a.hit("sub:cli", limit=3, window_seconds=60).allowed
    assert not worker_b.hit("sub:cli", limit=3, window_seconds=60).allowed


# ---------------------------------------------------------------------------
# Backend Redis
# ---------------------------------------------------------------------------
def test_redis_limiter_uses_atomic_counter_and_hash_tag():
    fake = FakeRedis()
    limiter = RedisRateLimiter(fake, fail_open=True)

    assert limiter.hit("sub:cli:abc", limit=2, window_seconds=60).allowed
    assert limiter.hit("sub:cli:abc", limit=2, window_seconds=60).allowed
    denied = limiter.hit("sub:cli:abc", limit=2, window_seconds=60)

    assert not denied.allowed
    assert len(fake.keys) == 3
    assert len(set(fake.keys)) == 1
    assert fake.keys[0].startswith("rl:{sub:cli}:")
    assert 1 <= denied.retry_after <= 61


def test_two_redis_limiters_share_the_counter():
    fake = FakeRedis()
    worker_a = RedisRateLimiter(fake)
    worker_b = RedisRateLimiter(fake)

    assert worker_a.hit("sub:cli", limit=2, window_seconds=60).allowed
    assert worker_b.hit("sub:cli", limit=2, window_seconds=60).allowed
    assert not worker_a.hit("sub:cli", limit=2, window_seconds=60).allowed


def test_redis_failure_fails_open_by_default():
    fake = FakeRedis()
    fake.fail = True
    limiter = RedisRateLimiter(fake, fail_open=True)

    result = limiter.hit("sub:cli", limit=1, window_seconds=60)
    assert result.allowed and result.degraded
    assert result.remaining == 1


def test_redis_failure_fails_closed_for_critical_requests():
    fake = FakeRedis()
    fake.fail = True
    limiter = RedisRateLimiter(fake, fail_open=True)

    result = limiter.hit("sub:cli", limit=5, window_seconds=60, critical=True)
    assert not result.allowed and result.degraded
    assert result.retry_after == 60


def test_redis_failure_can_fail_closed_globally():
    fake = FakeRedis()
    fake.fail = True
    limiter = RedisRateLimiter(fake, fail_open=False)

    assert not limiter.hit("sub:cli", limit=5, window_seconds=60).allowed


def test_build_rate_limiter_selects_backend(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    assert isinstance(build_rate_limiter(settings), InMemoryRateLimiter)

    fake = FakeRedis()
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    limiter = build_rate_limiter(settings, client=fake)
    assert isinstance(limiter, RedisRateLimiter)
    assert limiter.hit("sub:cli", limit=1, window_seconds=60).allowed


def test_redis_url_validation():
    with pytest.raises(ValueError):
        Settings(redis_url="http://localhost:6379")


def test_real_redis_client_degrades_when_server_is_unreachable():
    """Usa el paquete redis real contra un puerto cerrado: sin Redis de por medio."""
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(
        "redis://127.0.0.1:6399/0",
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )

    limiter = RedisRateLimiter(client, fail_open=True)
    allowed = limiter.hit("sub:cli", limit=5, window_seconds=60)
    assert allowed.allowed and allowed.degraded

    denied = limiter.hit("sub:cli", limit=5, window_seconds=60, critical=True)
    assert not denied.allowed and denied.degraded

    client.close()


def test_build_redis_client_returns_none_for_memory_backend():
    from app.redis_backend import build_redis_client

    settings = get_settings()
    assert build_redis_client(settings) is None


# ---------------------------------------------------------------------------
# Middleware: contrato HTTP idéntico con cualquier backend
# ---------------------------------------------------------------------------
class FakeLimiter:
    def __init__(self, *results: RateLimitResult) -> None:
        self.results = list(results)
        self.calls: list[tuple] = []

    def hit(self, key, limit, window_seconds, *, critical=False):
        self.calls.append((key, limit, window_seconds, critical))
        return self.results.pop(0)


def _app_with(limiter: FakeLimiter) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/ping")
    def health():
        return {"ok": True}

    @app.post("/api/v1/auth/token")
    def token():
        return {"access_token": "x"}

    @app.post("/api/v1/jobs")
    def jobs():
        return {"job_id": "job_test"}

    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    return app


def test_middleware_keeps_headers_on_success():
    result = RateLimitResult(True, 10, 9, 1_700_000_000, 0)
    limiter = FakeLimiter(result)
    with TestClient(_app_with(limiter)) as http:
        response = http.get("/api/v1/ping")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"
    assert response.headers["X-RateLimit-Reset"] == "1700000000"
    assert "X-RateLimit-Degraded" not in response.headers


def test_middleware_maps_denied_result_to_429_envelope():
    denied = RateLimitResult(False, 5, 0, 1_700_000_060, 42)
    limiter = FakeLimiter(denied)
    with TestClient(_app_with(limiter)) as http:
        response = http.get("/api/v1/ping")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["details"] == {
        "retry_after_seconds": 42,
        "limit": 5,
    }
    assert response.headers["Retry-After"] == "42"
    assert response.headers["X-RateLimit-Reset"] == "1700000060"


def test_middleware_marks_auth_requests_as_critical():
    result = RateLimitResult(True, 5, 4, 1_700_000_000, 0)
    limiter = FakeLimiter(result)
    with TestClient(_app_with(limiter)) as http:
        http.post("/api/v1/auth/token")

    assert limiter.calls[0][3] is True
    assert limiter.calls[0][1] == get_settings().auth_rate_limit_per_minute


def test_middleware_exposes_degraded_header():
    result = RateLimitResult(True, 10, 10, 1_700_000_000, 0, degraded=True)
    limiter = FakeLimiter(result)
    with TestClient(_app_with(limiter)) as http:
        response = http.get("/api/v1/ping")

    assert response.headers["X-RateLimit-Degraded"] == "1"


def test_middleware_uses_separate_buckets_per_limit(monkeypatch):
    """Regresión: las peticiones generales no deben agotar el bucket de jobs."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 600)
    monkeypatch.setattr(settings, "jobs_rate_limit_per_minute", 10)

    results = [RateLimitResult(True, limit, limit - 1, 1_700_000_000, 0) for limit in (600, 10)]
    limiter = FakeLimiter(*results)
    with TestClient(_app_with(limiter)) as http:
        http.get("/api/v1/ping")
        http.post("/api/v1/jobs")

    general_key, jobs_key = limiter.calls[0][0], limiter.calls[1][0]
    assert general_key.endswith(":general")
    assert jobs_key.endswith(":jobs")
    assert general_key != jobs_key


def test_forwarded_for_header_cannot_reset_the_bucket():
    """Regresión: X-Forwarded-For no es confiable; se usa request.client.host."""
    limiter = FakeLimiter(*[RateLimitResult(True, 10, 9, 1_700_000_000, 0)] * 3)
    with TestClient(_app_with(limiter)) as http:
        http.get("/api/v1/ping", headers={"X-Forwarded-For": "1.1.1.1"})
        http.get("/api/v1/ping", headers={"X-Forwarded-For": "2.2.2.2"})
        http.get("/api/v1/ping", headers={"X-Forwarded-For": "3.3.3.3"})

    keys = {call[0] for call in limiter.calls}
    assert len(keys) == 1
    assert next(iter(keys)).startswith("ip:")


def test_middleware_fail_closed_for_auth_with_unreachable_redis():
    """Con Redis caído: tráfico general degradado, /auth/token rechazado."""
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(
        "redis://127.0.0.1:6399/0",
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )
    limiter = RedisRateLimiter(client, fail_open=True)
    try:
        with TestClient(_app_with(limiter)) as http:
            general = http.get("/api/v1/ping")
            auth = http.post("/api/v1/auth/token")
    finally:
        client.close()

    assert general.status_code == 200
    assert general.headers["X-RateLimit-Degraded"] == "1"
    assert auth.status_code == 429
    assert auth.json()["error"]["code"] == "RATE_LIMITED"
    assert auth.headers["Retry-After"] == "60"
    assert auth.headers["X-RateLimit-Degraded"] == "1"
