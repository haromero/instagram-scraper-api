"""Regresiones del feedback loop: entrega de webhooks firmados con HMAC.

Primero fallan porque el servicio de entrega, la tabla de entregas y el
secreto generado aún no existen; después se corrige hasta dejarlas en verde.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import Base, SessionLocal, engine
from app.models import Job, Webhook

ROOT = Path(__file__).resolve().parents[1]
OWNER = "webhook_test_owner"
API_OWNER = "cli_test"
WEBHOOK_URL = "https://example.com/hooks/ig"
_CREATED_HOOK_IDS: list[str] = []


def _delivery_model():
    from app.models import WebhookDelivery

    return WebhookDelivery


def _webhooks_service():
    from app.services import webhooks as service

    return service


def _job(status: str = "completed") -> Job:
    db = SessionLocal()
    try:
        job = Job(
            id="job_webhooktest_" + uuid.uuid4().hex[:10],
            owner=OWNER,
            type="profile",
            target='{"username": "nasa"}',
            options="{}",
            status=status,
            attempts=0,
            items_scraped=1,
            items_total=1,
            result='[{"username": "nasa"}]',
            created_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc) if status == "completed" else None,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


def _hook(
    events=("job.completed",), secret: str = "hook-secret", owner: str = OWNER
) -> Webhook:
    db = SessionLocal()
    try:
        hook_id = "wh_" + uuid.uuid4().hex[:12]
        hook = Webhook(
            id=hook_id,
            owner=owner,
            url=WEBHOOK_URL,
            events=json.dumps(list(events)),
            secret=secret,
            active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(hook)
        db.commit()
        db.refresh(hook)
        _CREATED_HOOK_IDS.append(hook_id)
        return hook
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        db.query(Delivery).filter(Delivery.job_id.like("job_webhooktest_%")).delete(
            synchronize_session=False
        )
        if _CREATED_HOOK_IDS:
            db.query(Webhook).filter(Webhook.id.in_(_CREATED_HOOK_IDS)).delete(
                synchronize_session=False
            )
            _CREATED_HOOK_IDS.clear()
        db.query(Job).filter(Job.owner == OWNER).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Firma HMAC
# ---------------------------------------------------------------------------
def test_sign_uses_hmac_sha256_and_timestamp():
    service = _webhooks_service()
    secret = "secreto-de-prueba"
    timestamp = 1_700_000_000
    body = b'{"event":"job.completed"}'

    expected = "sha256=" + hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()

    assert service.sign(secret, timestamp, body) == expected
    assert service.SIGNATURE_HEADER == "X-Signature"


def test_verify_signature_accepts_valid_and_rejects_tampering():
    service = _webhooks_service()
    secret = "secreto-de-prueba"
    timestamp = int(time.time())
    body = b'{"event":"job.completed"}'
    signature = service.sign(secret, timestamp, body)

    assert service.verify_signature(secret, timestamp, body, signature) is True
    assert service.verify_signature(secret, timestamp, body + b" ", signature) is False
    assert service.verify_signature(secret, timestamp + 1, body, signature) is False
    assert service.verify_signature("otro", timestamp, body, signature) is False
    assert service.verify_signature(secret, timestamp, body, "sha256=deadbeef") is False
    assert service.verify_signature(secret, timestamp, body, "formato-invalido") is False


def test_verify_signature_rejects_replayed_old_timestamps():
    service = _webhooks_service()
    timestamp = int(time.time()) - 3600
    body = b"{}"
    signature = service.sign("secreto", timestamp, body)

    assert (
        service.verify_signature("secreto", timestamp, body, signature, max_age_seconds=300)
        is False
    )


# ---------------------------------------------------------------------------
# Secreto y contrato de creación
# ---------------------------------------------------------------------------
def test_webhook_creation_generates_secret_and_hides_it_on_list(client, auth_headers):
    created = client.post(
        "/api/v1/webhooks",
        headers=auth_headers,
        json={"url": WEBHOOK_URL, "events": ["job.completed"]},
    )
    assert created.status_code == 201, created.text
    secret = created.json().get("secret")
    assert secret and len(secret) >= 32
    webhook_id = created.json()["webhook_id"]

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT secret FROM webhooks WHERE id = :id"), {"id": webhook_id}
        ).scalar_one()
    assert stored.startswith("gAAAAA")
    assert secret not in stored

    listed = client.get("/api/v1/webhooks", headers=auth_headers)
    assert listed.status_code == 200
    own = [item for item in listed.json()["data"] if item["webhook_id"] == webhook_id]
    assert own and "secret" not in own[0]


def test_webhook_creation_keeps_caller_secret(client, auth_headers):
    created = client.post(
        "/api/v1/webhooks",
        headers=auth_headers,
        json={
            "url": WEBHOOK_URL,
            "events": ["job.failed"],
            "secret": "mi-secreto-de-32-caracteres-o-mas",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["secret"] == "mi-secreto-de-32-caracteres-o-mas"


# ---------------------------------------------------------------------------
# Entrega
# ---------------------------------------------------------------------------
def test_notify_sends_signed_payload_for_job_completed():
    service = _webhooks_service()
    hook = _hook()
    job = _job()
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with _mock_client(handler) as client:
        assert service.notify("job.completed", job.id, client=client) == 1

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == WEBHOOK_URL

    body = request.content
    payload = json.loads(body)
    assert payload["event"] == "job.completed"
    assert payload["data"]["job_id"] == job.id
    assert payload["data"]["type"] == "profile"
    assert payload["id"] == request.headers[service.DELIVERY_HEADER]

    timestamp = request.headers[service.TIMESTAMP_HEADER]
    assert service.verify_signature(
        "hook-secret", timestamp, body, request.headers[service.SIGNATURE_HEADER]
    )
    assert request.headers[service.EVENT_HEADER] == "job.completed"
    assert request.headers["Content-Type"] == "application/json"


def test_notify_only_delivers_subscribed_events():
    service = _webhooks_service()
    _hook(events=("job.failed",))
    job = _job()
    calls = []

    with _mock_client(lambda request: calls.append(request) or httpx.Response(204)) as client:
        assert service.notify("job.completed", job.id, client=client) == 0

    assert calls == []


def test_notify_is_idempotent_per_job_event_and_webhook():
    service = _webhooks_service()
    _hook()
    job = _job()
    calls = []

    with _mock_client(lambda request: calls.append(request) or httpx.Response(200)) as client:
        service.notify("job.completed", job.id, client=client)
        service.notify("job.completed", job.id, client=client)

    assert len(calls) == 1
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        rows = (
            db.query(Delivery)
            .filter(Delivery.job_id == job.id, Delivery.event == "job.completed")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "delivered"
        assert rows[0].attempts == 1
        assert rows[0].delivered_at is not None
    finally:
        db.close()


def test_notify_retries_transient_failures(monkeypatch):
    service = _webhooks_service()
    _hook()
    job = _job()
    sleeps = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: sleeps.append(seconds))
    calls = []
    responses = [httpx.Response(500), httpx.Response(503), httpx.Response(200)]

    def handler(_request):
        calls.append(1)
        return responses.pop(0)

    with _mock_client(handler) as client:
        assert service.notify("job.completed", job.id, client=client) == 1

    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        row = db.query(Delivery).filter(Delivery.job_id == job.id).one()
        assert row.status == "delivered"
        assert row.attempts == 3
        assert row.response_code == 200
    finally:
        db.close()


def test_notify_gives_up_after_max_attempts(monkeypatch):
    service = _webhooks_service()
    _hook(events=("job.failed",))
    job = _job()
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(503)

    with _mock_client(handler) as client:
        assert service.notify("job.failed", job.id, client=client) == 1

    assert len(calls) == 3
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        row = db.query(Delivery).filter(Delivery.job_id == job.id).one()
        assert row.status == "failed"
        assert row.attempts == 3
        assert row.response_code == 503
        assert row.delivered_at is None
    finally:
        db.close()


def test_notify_stops_when_delivery_budget_is_exhausted(monkeypatch):
    service = _webhooks_service()
    _hook()
    job = _job()
    calls = []
    sleeps = []

    class FakeTime:
        def __init__(self) -> None:
            self.values = iter([100.0, 100.0, 100.5, 111.0, 111.0, 111.0])

        def monotonic(self) -> float:
            return next(self.values)

        def time(self) -> float:
            return 100.0

        def sleep(self, seconds: float) -> None:
            sleeps.append(seconds)

    fake_time = FakeTime()
    monkeypatch.setattr(service, "time", fake_time)

    def handler(_request):
        calls.append(1)
        return httpx.Response(500)

    with _mock_client(handler) as client:
        service.notify("job.completed", job.id, client=client)

    assert len(calls) == 1
    assert sleeps == [1.0]
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        row = db.query(Delivery).filter(Delivery.job_id == job.id).one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert "presupuesto" in (row.error or "")
    finally:
        db.close()


def test_notify_blocks_ssrf_at_send_time(monkeypatch):
    service = _webhooks_service()
    _hook()
    job = _job()
    calls = []

    def _blocked(_url, **_):
        raise ValueError("El destino apunta a una red interna y está bloqueado.")

    monkeypatch.setattr(service, "assert_safe_outbound_url", _blocked)
    with _mock_client(lambda request: calls.append(request) or httpx.Response(200)) as client:
        service.notify("job.completed", job.id, client=client)

    assert calls == []
    Delivery = _delivery_model()
    db = SessionLocal()
    try:
        row = db.query(Delivery).filter(Delivery.job_id == job.id).one()
        assert row.status == "failed"
        assert "bloqueado" in (row.error or "")
    finally:
        db.close()


def test_delivery_failure_never_breaks_job_execution(monkeypatch):
    from app.services import jobs as jobs_service

    service = _webhooks_service()
    _hook()
    job = _job(status="queued")
    monkeypatch.setattr(jobs_service.time, "sleep", lambda _: None)
    monkeypatch.setattr(service, "_http_client", lambda: _mock_client(
        lambda _request: httpx.Response(500)
    ))
    monkeypatch.setattr(service.time, "sleep", lambda _: None)

    job_id = job.id
    jobs_service.run_job(job_id)

    db = SessionLocal()
    try:
        assert db.get(Job, job_id).status == "completed"
    finally:
        db.close()


def test_failed_job_dispatches_job_failed(monkeypatch):
    from app.services import jobs as jobs_service

    service = _webhooks_service()
    _hook(events=("job.failed",))
    job = _job(status="queued")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("fallo simulado del scraper")

    captured = []
    monkeypatch.setattr(jobs_service.scraper, "build_result", _boom)
    monkeypatch.setattr(service, "_http_client", lambda: _mock_client(
        lambda request: captured.append(request) or httpx.Response(200)
    ))

    jobs_service.run_job(job.id)

    assert len(captured) == 1
    assert json.loads(captured[0].content)["event"] == "job.failed"


def test_cancelled_job_dispatches_job_cancelled(client, auth_headers, monkeypatch):
    from app.services import jobs as jobs_service

    service = _webhooks_service()
    hook = _hook(events=("job.cancelled",), owner=API_OWNER)
    captured = []
    monkeypatch.setattr(jobs_service, "schedule", lambda _job_id: None)
    monkeypatch.setattr(service, "_http_client", lambda: _mock_client(
        lambda request: captured.append(request) or httpx.Response(200)
    ))

    created = client.post(
        "/api/v1/jobs",
        headers=auth_headers,
        json={"type": "profile", "target": {"username": "nasa"}},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    cancelled = client.delete(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.text

    assert len(captured) == 1
    assert json.loads(captured[0].content)["event"] == "job.cancelled"
    assert json.loads(captured[0].content)["data"]["job_id"] == job_id
    assert hook.id


# ---------------------------------------------------------------------------
# Persistencia, migración y despliegue
# ---------------------------------------------------------------------------
def test_webhook_delivery_model_and_migration(tmp_path):
    from app.db_migrations import upgrade_database

    Delivery = _delivery_model()
    assert "webhook_deliveries" in Base.metadata.tables
    assert "webhook_id" in Delivery.__table__.columns
    assert (ROOT / "alembic" / "versions" / "0002_webhook_deliveries.py").is_file()

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        upgrade_database(engine)
        assert "webhook_deliveries" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_baseline_creates_missing_delivery_table(tmp_path):
    from app.db_migrations import upgrade_database

    path = tmp_path / "legacy.db"
    legacy = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(legacy)
    with legacy.begin() as connection:
        connection.exec_driver_sql("DROP TABLE webhook_deliveries")
    legacy.dispose()

    upgrade_database(create_engine(f"sqlite:///{path}"))
    assert "webhook_deliveries" in inspect(path_to_engine(path)).get_table_names()


def path_to_engine(path):
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{path}")


def test_config_and_deployment_include_webhook_delivery():
    from app.config import Settings

    settings = Settings()
    assert settings.webhook_timeout_seconds >= 1
    assert 1 <= settings.webhook_max_attempts <= 10
    assert settings.webhook_backoff_seconds >= 0
    assert settings.webhook_delivery_budget_seconds >= 1

    deployment = (ROOT / "scripts" / "check_deployment.py").read_text(encoding="utf-8")
    assert "app/services/webhooks.py" in deployment
    assert "0002_webhook_deliveries.py" in deployment
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "aún no está implementada" not in readme
