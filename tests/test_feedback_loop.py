"""Regresiones del feedback loop de los últimos cambios.

Cada prueba reproduce un fallo concreto detectado al auditar rate limit,
revocación, candados, caché y backend Bluesky. Se añaden primero (fallan) y
después se corrige el código hasta que pasan.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import revocation
from app.config import Settings, get_settings
from app.errors import APIError
from app.locks import LocalLock, RedisLock
from app.middleware import RateLimitMiddleware, is_valid_request_id
from app.ratelimit import InMemoryRateLimiter, RateLimitResult, RedisRateLimiter
from app.revocation import RedisRevocationStore, build_revocation_store
from app.services import scraper
from app.services.scraper_bluesky import BlueskyScraper, _map_user
from app.security import create_access_token
from starlette.requests import Request

HANDLE = "alice.bsky.social"


class _AppLimiter:
    """Limiter de prueba que solo registra las claves que recibe."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def hit(self, key, limit, window_seconds, *, critical=False):
        self.keys.append(key)
        return RateLimitResult(True, limit, limit - 1, 1_700_000_000, 0)


def _app_with(limiter) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/v1/jobs")
    def jobs():
        return {"job_id": "job_test"}

    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    return app


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
def test_equal_limits_do_not_share_a_bucket(monkeypatch):
    """Regresión: el sufijo debe ser el tipo de cubo, no el valor del límite."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10)
    monkeypatch.setattr(settings, "jobs_rate_limit_per_minute", 10)

    limiter = _AppLimiter()
    with TestClient(_app_with(limiter)) as http:
        http.get("/api/v1/ping")
        http.post("/api/v1/jobs")

    assert len(set(limiter.keys)) == 2, limiter.keys
    assert any(":general" in key for key in limiter.keys)
    assert any(":jobs" in key for key in limiter.keys)


def test_memory_limiter_evicts_when_max_keys_is_exceeded():
    """Regresión: _MAX_KEYS debe ser un techo real, no solo purga de expirados."""
    limiter = InMemoryRateLimiter(max_keys=10)
    for index in range(25):
        limiter.hit(f"sub:cli-{index}", limit=1, window_seconds=60)

    assert len(limiter._store) <= 10


def test_request_id_rejects_trailing_newline():
    """Regresión: `$` acepta un LF final; la cabecera debe validarse completa."""
    assert is_valid_request_id("req-abcdefgh")
    assert not is_valid_request_id("req-abcdefgh\n")
    assert not is_valid_request_id("req-abc\r\ndefgh")


class _SlidingRedis:
    """ZSET mínimo para emular la ventana deslizante del script Lua."""

    def __init__(self) -> None:
        self.data: dict[str, list[tuple[float, str]]] = {}
        self.script = ""

    def eval(self, script, numkeys, key, now_ms, window_ms, member, limit):
        self.script = script
        now = float(now_ms)
        window = float(window_ms)
        entries = [(score, name) for score, name in self.data.get(key, []) if score > now - window]
        allowed = 0
        if len(entries) < int(limit):
            entries.append((now, str(member)))
            allowed = 1
        self.data[key] = entries
        oldest = min((score for score, _ in entries), default=now)
        return [allowed, len(entries), oldest, window]


def test_redis_uses_sliding_window_at_bucket_boundary(monkeypatch):
    """Regresión: 2 hits al final de una ventana + 2 justo después = 4 en 0,2 s."""
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("app.ratelimit.time.time", lambda: clock["t"])
    fake = _SlidingRedis()
    limiter = RedisRateLimiter(fake)

    assert limiter.hit("sub:cli", limit=2, window_seconds=1).allowed
    assert limiter.hit("sub:cli", limit=2, window_seconds=1).allowed
    clock["t"] += 0.9
    denied = limiter.hit("sub:cli", limit=2, window_seconds=1)
    assert not denied.allowed
    assert 1 <= denied.retry_after <= 2
    clock["t"] += 0.2
    assert limiter.hit("sub:cli", limit=2, window_seconds=1).allowed
    assert "ZADD" in fake.script and "ZREMRANGEBYSCORE" in fake.script


def test_redis_hash_tag_is_shared_by_all_buckets_of_a_client():
    """El hash tag debe ser el cliente, no el tipo de cubo."""
    fake = _SlidingRedis()
    limiter = RedisRateLimiter(fake)
    for key in ("ip:testclient:general", "ip:testclient:auth", "ip:testclient:jobs"):
        limiter.hit(key, limit=2, window_seconds=60)

    tags = {name.split("{", 1)[1].split("}", 1)[0] for name in fake.data}
    assert tags == {"ip:testclient"}, tags


def _request_with(peer: str, forwarded: str | None = None, token: str | None = None) -> Request:
    headers = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_trusted_proxy_uses_forwarded_client(monkeypatch):
    """Detrás de un ingress, el cubo debe ser el cliente real, no el proxy."""
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    middleware = RateLimitMiddleware(FastAPI(), limiter=_AppLimiter())

    request = _request_with("10.1.2.3", "203.0.113.5, 10.1.2.3")
    assert middleware._client_key(request) == "ip:203.0.113.5"


def test_untrusted_proxy_ignores_forwarded_header(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxies", "")
    middleware = RateLimitMiddleware(FastAPI(), limiter=_AppLimiter())

    request = _request_with("10.1.2.3", "203.0.113.5")
    assert middleware._client_key(request) == "ip:10.1.2.3"


def test_rate_limit_key_includes_tenant():
    """Dos tenants con el mismo sub no deben compartir cupo."""
    middleware = RateLimitMiddleware(FastAPI(), limiter=_AppLimiter())
    token_a, _ = create_access_token("cli_shared", ["profiles:read"], tenant="tenant-a")
    token_b, _ = create_access_token("cli_shared", ["profiles:read"], tenant="tenant-b")

    key_a = middleware._client_key(_request_with("127.0.0.1", token=token_a))
    key_b = middleware._client_key(_request_with("127.0.0.1", token=token_b))

    assert key_a != key_b
    assert "tenant-a" in key_a and "tenant-b" in key_b


# ---------------------------------------------------------------------------
# Revocación
# ---------------------------------------------------------------------------
def test_revoke_during_redis_outage_is_not_lost():
    """Una revocación hecha mientras Redis cae debe aplicarse al recuperarse."""
    fake = _FakeRedisRevocation()
    store = RedisRevocationStore(fake, fail_open=True)
    fake.fail = True

    store.revoke("jti-outage", time.time() + 60)

    assert store.is_revoked("jti-outage")
    assert not store.is_revoked("jti-other")
    fake.fail = False
    assert store.is_revoked("jti-outage"), "la revocación no se propagó a Redis"
    assert "rev:jti-outage" in fake.data


class _FakeRedisRevocation:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.fail = False

    def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis caído")
        self.data[key] = (value, time.time() + ex)

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis caído")
        entry = self.data.get(key)
        return int(bool(entry) and entry[1] > time.time())

    def delete(self, key):
        self.data.pop(key, None)


def test_revocation_store_defaults_to_fail_closed(monkeypatch):
    """Un JWT del que no podemos comprobar su revocación no debe aceptarse."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    store = build_revocation_store(settings, client=_FakeRedisRevocation())
    assert store._fail_open is False


# ---------------------------------------------------------------------------
# Candados
# ---------------------------------------------------------------------------
class _ExpiringRedis:
    """Redis con expiración por reloj real para probar la renovación del TTL."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}

    def set(self, key, value, nx=False, px=None):
        entry = self.data.get(key)
        if entry and entry[1] > time.time():
            return False
        self.data[key] = (value, time.time() + px / 1000)
        return True

    def pexpire(self, key, px):
        entry = self.data.get(key)
        if not entry:
            return False
        self.data[key] = (entry[0], time.time() + px / 1000)
        return True

    def eval(self, script, numkeys, key, token):
        entry = self.data.get(key)
        if entry and entry[0] == token:
            self.data.pop(key, None)
            return 1
        return 0


def test_lock_ttl_is_renewed_during_long_critical_section():
    """Un job lento no debe dejar expirar el candado y abrir la sección a otro."""
    fake = _ExpiringRedis()
    owner = RedisLock(fake, ttl_seconds=0.15, timeout_seconds=0.0, heartbeat_seconds=0.05)
    other = RedisLock(fake, ttl_seconds=0.15, timeout_seconds=0.0)

    with owner.hold("job-quota:cli"):
        time.sleep(0.45)
        with pytest.raises(APIError) as exc:
            with other.hold("job-quota:cli"):
                pass
        assert exc.value.code == "CONCURRENCY_LIMIT"


def test_concurrency_error_does_not_expose_lock_name():
    fake = _ExpiringRedis()
    first = RedisLock(fake, ttl_seconds=0.3, timeout_seconds=0.0)
    second = RedisLock(fake, ttl_seconds=0.3, timeout_seconds=0.0)

    with first.hold("job-quota:secreto"):
        with pytest.raises(APIError) as exc:
            with second.hold("job-quota:secreto"):
                pass

    assert "job-quota" not in str(exc.value.details)


def test_quota_lock_timeouts_are_configurable(monkeypatch):
    from app.locks import build_quota_lock
    from app.locks import RedisLock as _RedisLock

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    monkeypatch.setattr(settings, "job_lock_ttl_seconds", 4.0)
    monkeypatch.setattr(settings, "job_lock_timeout_seconds", 0.5)
    lock = build_quota_lock(settings, client=_FakeRedisRevocation())

    assert isinstance(lock, _RedisLock)
    assert lock._ttl_ms == 4000
    assert lock._timeout_seconds == 0.5


def test_local_lock_is_per_name():
    """El backend memory también debe serializar por owner, como Redis."""
    lock = LocalLock()
    active = 0
    peak = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        nonlocal active, peak
        barrier.wait()
        with lock.hold(name):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 2


# ---------------------------------------------------------------------------
# Backend Bluesky
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, expected",
    [("valid", True), ("invalid", False), ("expired", False), ("self", False), (None, False)],
)
def test_is_verified_only_for_valid_status(status, expected):
    profile = {"did": "did:plc:x", "handle": HANDLE, "verification": {"verifiedStatus": status}}
    assert _map_user(profile)["is_verified"] is expected


def test_401_on_last_attempt_refreshes_session():
    """Con max_retries=1 un accessJwt caducado debe renovarse y reintentar."""
    sessions = {"n": 0}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("createSession"):
            sessions["n"] += 1
            return httpx.Response(200, json={"accessJwt": f"jwt-{sessions['n']}"})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"message": "ExpiredToken"})
        return httpx.Response(200, json={"ok": True})

    backend = BlueskyScraper(
        handle=HANDLE,
        app_password="pw",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_seconds=0,
    )
    result = backend._request("app.bsky.feed.getAuthorFeed", {}, authenticated=True)

    assert result == {"ok": True}
    assert sessions["n"] == 2


def test_reset_backend_closes_http_client(monkeypatch):
    backend = BlueskyScraper(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    closed = []
    original_close = backend.close
    backend.close = lambda: (closed.append(1), original_close())
    monkeypatch.setattr(scraper, "_backend", backend)

    scraper.reset_backend()

    assert closed == [1]


# ---------------------------------------------------------------------------
# Caché de perfiles
# ---------------------------------------------------------------------------
def test_profile_cache_anchor_is_cache_time_not_scraped_at(client, auth_headers, monkeypatch):
    """Un backend con timestamp viejo no debe dejar la caché caducada al escribirla."""
    settings = get_settings()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    calls = {"n": 0}

    def fake_profile(username: str) -> dict:
        calls["n"] += 1
        return {
            "username": username,
            "user_id": "1",
            "full_name": "TTL",
            "biography": None,
            "is_private": False,
            "is_verified": False,
            "followers_count": calls["n"],
            "following_count": 0,
            "posts_count": 0,
            "profile_pic_url": None,
            "external_url": None,
            "scraped_at": old,
        }

    monkeypatch.setattr(scraper, "get_profile", fake_profile)
    monkeypatch.setattr(settings, "profile_cache_ttl_seconds", 3600)
    username = "ttl_anchor_" + uuid.uuid4().hex[:8]

    first = client.get(f"/api/v1/profiles/{username}", headers=auth_headers)
    second = client.get(f"/api/v1/profiles/{username}", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["n"] == 1


def test_month_quota_uses_utc_month_boundary(monkeypatch):
    """Los jobs del mes anterior no deben consumir la cuota del actual."""
    from app.database import SessionLocal
    from app.models import Job as JobModel
    from app.services import jobs as jobs_service

    monkeypatch.setattr(jobs_service, "schedule", lambda job_id: None)
    owner = "cli_month_edge_" + uuid.uuid4().hex[:8]
    previous_month = datetime.now(timezone.utc).replace(day=1) - timedelta(seconds=1)
    db = SessionLocal()
    try:
        db.add(
            JobModel(
                id="job_edge_" + uuid.uuid4().hex[:12],
                owner=owner,
                type="profile",
                target="{}",
                options="{}",
                status="queued",
                items_scraped=0,
                items_total=0,
                created_at=previous_month,
                estimated_completion=previous_month,
            )
        )
        db.commit()
        jobs_service._assert_quota(db, owner)  # no debe lanzar
    finally:
        db.query(JobModel).filter(JobModel.owner == owner).delete()
        db.commit()
        db.close()


def test_health_ready_reports_real_queue_state(client, monkeypatch):
    """El readiness debe reflejar el pool, no devolver valores hardcodeados."""
    from app.routers import health
    from app.services import jobs as jobs_service

    monkeypatch.setattr(health, "get_client", lambda: None)
    monkeypatch.setattr(
        jobs_service,
        "pool_status",
        lambda: {"active": 3, "idle": 0, "max_workers": 3, "shutdown": False},
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["workers"] == {"active": 3, "idle": 0}
    assert checks["queue"] == "degraded"
    assert checks["proxies"]["total"] >= 0


def test_health_ready_fails_when_pool_is_shutdown(client, monkeypatch):
    from app.routers import health
    from app.services import jobs as jobs_service

    monkeypatch.setattr(health, "get_client", lambda: None)
    monkeypatch.setattr(
        jobs_service,
        "pool_status",
        lambda: {"active": 0, "idle": 0, "max_workers": 3, "shutdown": True},
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"queue": "down"}


def test_profile_cache_ignores_future_scraped_at(client, auth_headers, monkeypatch):
    """El scraped_at del backend no afecta al TTL: se ancla al momento de cachear."""
    settings = get_settings()
    future = datetime.now(timezone.utc) + timedelta(days=1)
    calls = {"n": 0}

    def fake_profile(username: str) -> dict:
        calls["n"] += 1
        return {
            "username": username,
            "user_id": "1",
            "full_name": "TTL",
            "biography": None,
            "is_private": False,
            "is_verified": False,
            "followers_count": calls["n"],
            "following_count": 0,
            "posts_count": 0,
            "profile_pic_url": None,
            "external_url": None,
            "scraped_at": future,
        }

    monkeypatch.setattr(scraper, "get_profile", fake_profile)
    monkeypatch.setattr(settings, "profile_cache_ttl_seconds", 3600)
    username = "ttl_future_" + uuid.uuid4().hex[:8]

    client.get(f"/api/v1/profiles/{username}", headers=auth_headers)
    client.get(f"/api/v1/profiles/{username}", headers=auth_headers)

    assert calls["n"] == 1
