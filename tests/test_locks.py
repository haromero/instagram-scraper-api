"""Tests de los candados distribuidos y la cuota atómica de jobs."""

import threading
import time
import uuid

import pytest

from app import locks
from app.config import get_settings
from app.database import SessionLocal
from app.errors import APIError
from app.locks import LocalLock, RedisLock, build_quota_lock
from app.models import Job
from app.routers import health
from app.services import jobs as jobs_service


class FakeRedis:
    """Implementa solo SET NX PX, EVAL (liberación) y PING."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.fail = False
        self.pings = 0

    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise RuntimeError("redis caído")
        assert nx and px
        now = time.time()
        entry = self.data.get(key)
        if entry and entry[1] > now:
            return False
        self.data[key] = (value, now + px / 1000)
        return True

    def eval(self, script, numkeys, key, token):
        assert numkeys == 1 and "del" in script
        if self.fail:
            raise RuntimeError("redis caído")
        entry = self.data.get(key)
        if entry and entry[0] == token:
            del self.data[key]
            return 1
        return 0

    def pexpire(self, key, px):
        entry = self.data.get(key)
        if not entry:
            return False
        self.data[key] = (entry[0], time.time() + px / 1000)
        return True

    def ping(self):
        self.pings += 1
        if self.fail:
            raise RuntimeError("redis caído")
        return True


# ---------------------------------------------------------------------------
# Candado local
# ---------------------------------------------------------------------------
def test_local_lock_serializes_critical_sections():
    lock = LocalLock()
    active = 0
    peak = 0
    guard = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        nonlocal active, peak
        barrier.wait()
        with lock.hold("job-quota:cli"):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1


# ---------------------------------------------------------------------------
# Candado Redis
# ---------------------------------------------------------------------------
def test_two_redis_locks_exclude_each_other():
    fake = FakeRedis()
    lock_a = RedisLock(fake, timeout_seconds=0.1)
    lock_b = RedisLock(fake, timeout_seconds=0.1)

    with lock_a.hold("job-quota:cli"):
        with pytest.raises(APIError) as exc:
            with lock_b.hold("job-quota:cli"):
                pass
        assert exc.value.status_code == 429
        assert exc.value.code == "CONCURRENCY_LIMIT"
        assert exc.value.headers["Retry-After"] == "1"

    # Tras liberar, otro dueño puede adquirirlo.
    with lock_b.hold("job-quota:cli"):
        pass


def test_redis_lock_does_not_release_another_owner_lock():
    fake = FakeRedis()
    lock_a = RedisLock(fake, timeout_seconds=0.1)
    lock_b = RedisLock(fake, timeout_seconds=0.1)

    with lock_a.hold("job-quota:cli"):
        # Simula que el primer candado expiró y el segundo lo tomó.
        fake.data.clear()
    with lock_b.hold("job-quota:cli"):
        assert "lock:job-quota:cli" in fake.data

    assert "lock:job-quota:cli" not in fake.data


def test_redis_lock_fails_open_and_fails_closed():
    fake = FakeRedis()
    fake.fail = True

    with RedisLock(fake, fail_open=True).hold("job-quota:cli"):
        pass

    with pytest.raises(APIError) as exc:
        with RedisLock(fake, fail_open=False).hold("job-quota:cli"):
            pass
    assert exc.value.status_code == 503
    assert exc.value.code == "SERVICE_UNAVAILABLE"


def test_build_quota_lock_selects_backend(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    assert isinstance(build_quota_lock(settings), LocalLock)

    fake = FakeRedis()
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    lock = build_quota_lock(settings, client=fake)
    assert isinstance(lock, RedisLock)
    with lock.hold("job-quota:cli"):
        assert "lock:job-quota:cli" in fake.data


# ---------------------------------------------------------------------------
# Cuota de jobs: dos "workers" compartiendo el candado Redis
# ---------------------------------------------------------------------------
def test_job_quota_is_atomic_across_workers(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "concurrent_jobs_limit", 1)
    monkeypatch.setattr(settings, "job_simulation_seconds", 30.0)
    monkeypatch.setattr(settings, "job_steps", 100)

    fake = FakeRedis()
    monkeypatch.setattr(locks, "quota_lock", RedisLock(fake, timeout_seconds=2))
    # Sin ejecución en segundo plano: el test solo verifica la cuota.
    monkeypatch.setattr(jobs_service, "schedule", lambda job_id: None)

    owner = "cli_atomic_" + uuid.uuid4().hex[:8]
    barrier = threading.Barrier(2)
    outcomes: dict[int, tuple[str, str]] = {}

    def worker(index: int) -> None:
        db = SessionLocal()
        try:
            barrier.wait()
            job = jobs_service.enqueue(db, owner, "profile", {"username": "nasa"})
            outcomes[index] = ("ok", job.id)
        except APIError as exc:
            outcomes[index] = ("error", exc.code)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    results = sorted(outcomes.values())
    assert results[0] == ("error", "CONCURRENCY_LIMIT")
    assert results[1][0] == "ok"

    # Limpieza: cancelar el job ganador para no dejar un hilo simulando 30 s.
    db = SessionLocal()
    try:
        job = db.get(Job, results[1][1])
        job.status = "cancelled"
        db.commit()
    finally:
        db.close()


def test_monthly_request_quota_is_enforced(monkeypatch):
    """Cada job consume 10 peticiones: con 20 de cuota solo caben 2."""
    settings = get_settings()
    monkeypatch.setattr(settings, "concurrent_jobs_limit", 10)
    monkeypatch.setattr(settings, "requests_limit", 20)
    monkeypatch.setattr(locks, "quota_lock", RedisLock(FakeRedis(), timeout_seconds=2))
    monkeypatch.setattr(jobs_service, "schedule", lambda job_id: None)

    owner = "cli_monthly_" + uuid.uuid4().hex[:8]
    try:
        for _ in range(2):
            db = SessionLocal()
            try:
                jobs_service.enqueue(db, owner, "profile", {"username": "nasa"})
            finally:
                db.close()

        db = SessionLocal()
        try:
            with pytest.raises(APIError) as exc:
                jobs_service.enqueue(db, owner, "profile", {"username": "nasa"})
        finally:
            db.close()

        assert exc.value.code == "QUOTA_EXCEEDED"
        assert exc.value.details == {"requests_used": 20, "requests_limit": 20}
    finally:
        db = SessionLocal()
        try:
            db.query(Job).filter(Job.owner == owner).delete()
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Readiness de Redis
# ---------------------------------------------------------------------------
def test_health_ready_reports_redis_when_configured(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(health, "get_client", lambda: fake)

    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["redis"] == "ok"
    assert fake.pings == 1

    fake.fail = True
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["details"] == {"redis": "down"}


def test_health_ready_omits_redis_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(health, "get_client", lambda: None)
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert "redis" not in response.json()["checks"]
