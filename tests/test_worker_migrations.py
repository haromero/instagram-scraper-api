"""Regresiones del feedback loop: Alembic, cola Redis y worker real.

Primero fallan porque Alembic y el worker aún no existen; después se corrige
la implementación hasta que toda la suite quede en verde.
"""

import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
import yaml
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import Job

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> Settings:
    return Settings(rate_limit_backend="redis", job_backend="redis")


def _db_job(db: Session, owner: str = "worker_test") -> Job:
    job = Job(
        id="job_" + os.urandom(6).hex(),
        owner=owner,
        type="profile",
        target='{"username": "test"}',
        options="{}",
        status="queued",
        items_scraped=0,
        items_total=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Alembic
# ---------------------------------------------------------------------------
def test_alembic_files_exist():
    assert (ROOT / "alembic.ini").is_file()
    assert (ROOT / "alembic" / "env.py").is_file()
    assert list((ROOT / "alembic" / "versions").glob("*_initial.py"))


def test_alembic_creates_full_schema(tmp_path):
    from app.db_migrations import upgrade_database

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    upgrade_database(engine)

    tables = set(inspect(engine).get_table_names())
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert "attempts" in {c["name"] for c in inspect(engine).get_columns("jobs")}


def test_alembic_baselines_legacy_sqlite_db(tmp_path):
    from app.db_migrations import upgrade_database

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE jobs DROP COLUMN attempts")

    upgrade_database(engine)

    assert "attempts" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "alembic_version" in inspect(engine).get_table_names()


def test_models_and_requirements_include_new_dependencies():
    assert "attempts" in Job.__table__.columns
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "alembic" in requirements
    assert "fakeredis" in requirements


# ---------------------------------------------------------------------------
# Configuración y cola Redis
# ---------------------------------------------------------------------------
def test_job_backend_defaults_to_thread_and_validates_redis(monkeypatch):
    monkeypatch.delenv("IG_JOB_BACKEND", raising=False)
    assert Settings().job_backend == "thread"

    monkeypatch.setenv("IG_JOB_BACKEND", "redis")
    monkeypatch.setenv("IG_RATE_LIMIT_BACKEND", "memory")
    with pytest.raises(ValueError, match="IG_JOB_BACKEND"):
        Settings()


def test_redis_queue_publish_claim_ack_and_status():
    from app.queue import RedisJobQueue

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-api")

    queue.publish("job_queue_1")
    claimed = queue.claim()

    assert [item.job_id for item in claimed] == ["job_queue_1"]
    assert claimed[0].attempts == 0
    assert queue.status()["pending"] == 1
    assert queue.status()["backend"] == "redis"

    queue.ack(claimed[0].message_id)
    assert queue.status()["pending"] == 0


def test_redis_queue_retry_requeues_with_attempts():
    from app.queue import RedisJobQueue

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-api")

    queue.publish("job_queue_2")
    item = queue.claim()[0]
    queue.retry(item.message_id, item.job_id, item.attempts)

    retried = queue.claim()[0]
    assert retried.job_id == "job_queue_2"
    assert retried.attempts == 1
    assert queue.status()["pending"] == 1


def test_redis_queue_dead_letter():
    from app.queue import RedisJobQueue

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-api")

    queue.publish("job_queue_3")
    item = queue.claim()[0]
    queue.dead_letter(item.message_id, item.job_id, item.attempts, "boom")

    dead = client.xrange(f"{settings.job_queue_stream}:dead")
    assert len(dead) == 1
    assert dead[0][1]["job_id"] == "job_queue_3"
    assert queue.status()["pending"] == 0


# ---------------------------------------------------------------------------
# Worker real
# ---------------------------------------------------------------------------
def test_worker_completes_and_acks_job(monkeypatch):
    from app.database import SessionLocal
    from app.queue import RedisJobQueue
    from app.services import jobs as jobs_service
    from app.worker import Worker

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-worker")

    db = SessionLocal()
    try:
        job = _db_job(db)
        queue.publish(job.id)

        def _complete(job_id: str, **_: object) -> None:
            current = db.get(Job, job_id)
            current.status = "completed"
            db.commit()

        monkeypatch.setattr(jobs_service, "run_job", _complete)
        processed = Worker(queue, settings).process_once()

        assert processed == 1
        assert db.get(Job, job.id).status == "completed"
        assert queue.status()["pending"] == 0
    finally:
        db.query(Job).filter(Job.owner == "worker_test").delete()
        db.commit()
        db.close()


def test_worker_retries_failed_job(monkeypatch):
    from app.database import SessionLocal
    from app.queue import RedisJobQueue
    from app.services import jobs as jobs_service
    from app.worker import Worker

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-worker")

    db = SessionLocal()
    try:
        job = _db_job(db)
        queue.publish(job.id)

        def _boom(job_id: str, **_: object) -> None:
            raise RuntimeError("transient")

        monkeypatch.setattr(jobs_service, "run_job", _boom)
        assert Worker(queue, settings).process_once() == 1

        assert db.get(Job, job.id).status == "queued"
        assert queue.status()["stream_length"] == 2
        assert queue.claim()[0].attempts == 1
    finally:
        db.query(Job).filter(Job.owner == "worker_test").delete()
        db.commit()
        db.close()


def test_worker_dead_letters_after_max_attempts(monkeypatch):
    from app.database import SessionLocal
    from app.queue import RedisJobQueue
    from app.services import jobs as jobs_service
    from app.worker import Worker

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-worker")

    db = SessionLocal()
    try:
        job = _db_job(db)
        client.xadd(
            settings.job_queue_stream,
            {"job_id": job.id, "attempts": str(settings.job_max_attempts)},
        )

        def _boom(job_id: str, **_: object) -> None:
            raise RuntimeError("permanent")

        monkeypatch.setattr(jobs_service, "run_job", _boom)
        assert Worker(queue, settings).process_once() == 1

        db.refresh(job)
        assert job.status == "failed"
        assert len(client.xrange(f"{settings.job_queue_stream}:dead")) == 1
    finally:
        db.query(Job).filter(Job.owner == "worker_test").delete()
        db.commit()
        db.close()


def test_worker_reclaims_stuck_running_job(monkeypatch):
    from app.database import SessionLocal
    from app.queue import RedisJobQueue
    from app.services import jobs as jobs_service
    from app.worker import Worker

    settings = _settings()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(client, settings, consumer_name="test-worker")

    db = SessionLocal()
    try:
        job = _db_job(db)
        job.status = "running"
        job.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        queue.publish(job.id)

        seen = {}

        def _capture(job_id: str, **_: object) -> None:
            db.expire_all()  # el worker usa otra sesión
            current = db.get(Job, job_id)
            seen["status"] = current.status
            seen["attempts"] = current.attempts
            current.status = "completed"
            db.commit()

        monkeypatch.setattr(jobs_service, "run_job", _capture)
        Worker(queue, settings).process_once()

        assert seen["status"] == "queued"
        assert seen["attempts"] == 1
    finally:
        db.query(Job).filter(Job.owner == "worker_test").delete()
        db.commit()
        db.close()


def test_readiness_reports_redis_queue_backend(client, monkeypatch):
    from app.routers import health
    from app.services import jobs as jobs_service
    from app.queue import RedisJobQueue

    settings = _settings()
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(fake_client, settings, consumer_name="test-api")
    monkeypatch.setattr(jobs_service, "_queue", queue)
    monkeypatch.setattr(health, "get_client", lambda: fake_client)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["queue_backend"] == "redis"
    assert checks["workers"]["active"] == 0


# ---------------------------------------------------------------------------
# Despliegue del worker
# ---------------------------------------------------------------------------
def test_compose_and_render_include_worker():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "worker" in services
    assert services["worker"]["command"] == ["python", "-m", "app.worker"]
    assert services["worker"]["environment"]["IG_JOB_BACKEND"] == "redis"
    assert services["api"]["environment"]["IG_JOB_BACKEND"] == "redis"

    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    web = next(item for item in blueprint["services"] if item["type"] == "web")
    env = {item["key"]: item for item in web["envVars"]}
    assert env["IG_JOB_BACKEND"]["value"] == "redis"
    assert web["dockerCommand"] == "python -m app.launch"


def test_launcher_starts_worker_and_web_without_leaking_processes():
    from app import launch

    calls = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.terminated = False
            self.returncode = 0

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        calls.append(command)
        return FakeProcess(100 + len(calls))

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(launch.signal, "signal", lambda *_: None)
        monkeypatch.setattr(launch.sys, "exit", lambda *_: None)
        monkeypatch.setenv("IG_JOB_BACKEND", "redis")
        monkeypatch.setenv("PORT", "8123")

        launch.main()

        assert any("app.worker" in " ".join(cmd) for cmd in calls)
        assert any("gunicorn" in " ".join(cmd) for cmd in calls)
        assert all("8123" in " ".join(cmd) for cmd in calls if "gunicorn" in " ".join(cmd))
    finally:
        monkeypatch.undo()


def test_worker_module_is_executable():
    result = subprocess.run(
        [sys.executable, "-c", "import app.worker; assert hasattr(app.worker, 'main')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
