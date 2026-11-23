"""Pruebas de regresión del feedback loop de hardening.

Cada prueba verifica el resultado esperado de una corrección concreta, no solo
que el código "no explote".
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings, get_settings
from app.database import SessionLocal, engine
from app.middleware import MaxBodySizeMiddleware
from app.models import Job as JobModel
from app.models import Proxy as ProxyModel
from app.passwords import DUMMY_HASH, hash_secret, verify_secret
from app.security import create_access_token, revoke_token
from app.services import jobs as jobs_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _token(sub: str, scopes: list[str], *, tenant: str | None = None) -> dict[str, str]:
    token, _ = create_access_token(sub, scopes, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


def _raw_token(**overrides) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "cli_test",
        "tenant": "cli_test",
        "scopes": ["profiles:read"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": uuid.uuid4().hex,
    }
    payload.update(overrides)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _wait_job(client, headers, job_id: str, statuses: set[str], timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        if response.status_code == 200:
            last = response.json()
            if last["status"] in statuses:
                return last
        time.sleep(0.05)
    raise AssertionError(f"El job {job_id} no llegó a {statuses}; último estado: {last}")


def _valid_settings_kwargs() -> dict:
    return {
        "jwt_secret": "0123456789abcdefghijklmnopqrstuvwxyzABCDEF",
        "encryption_key": Fernet.generate_key().decode(),
        "client_id": "cli_config_test",
        "client_secret_hash": DUMMY_HASH,
        "admin_client_id": "cli_admin_config",
        "admin_client_secret_hash": DUMMY_HASH,
        "client_scopes": "profiles:read,jobs:read",
    }


# ---------------------------------------------------------------------------
# Configuración fail-fast
# ---------------------------------------------------------------------------
def test_config_rejects_admin_scope_for_normal_client():
    kwargs = _valid_settings_kwargs()
    kwargs["client_scopes"] = "profiles:read,admin:write"
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_config_rejects_unknown_or_duplicate_scopes():
    kwargs = _valid_settings_kwargs()
    for scopes in ("profiles:read,scopes:inventados", "profiles:read,profiles:read"):
        with pytest.raises(ValidationError):
            Settings(**{**kwargs, "client_scopes": scopes})


def test_config_requires_admin_id_and_hash_together():
    kwargs = _valid_settings_kwargs()
    with pytest.raises(ValidationError):
        Settings(**{**kwargs, "admin_client_secret_hash": None})
    with pytest.raises(ValidationError):
        Settings(**{**kwargs, "admin_client_id": None})


def test_config_rejects_admin_id_equal_to_client_id():
    kwargs = _valid_settings_kwargs()
    with pytest.raises(ValidationError):
        Settings(**{**kwargs, "admin_client_id": kwargs["client_id"]})


def test_config_bounds_job_simulation_values():
    kwargs = _valid_settings_kwargs()
    for override in ({"job_steps": 0}, {"job_steps": 1001}, {"job_simulation_seconds": -1}):
        with pytest.raises(ValidationError):
            Settings(**{**kwargs, **override})


def test_config_rejects_unsafe_jwt_algorithm_and_empty_issuer():
    kwargs = _valid_settings_kwargs()
    for override in (
        {"jwt_algorithm": "none"},
        {"jwt_algorithm": "RS256"},
        {"jwt_issuer": ""},
        {"jwt_audience": ""},
    ):
        with pytest.raises(ValidationError):
            Settings(**{**kwargs, **override})


def test_config_requires_explicit_allowed_hosts_without_wildcard():
    kwargs = _valid_settings_kwargs()
    for hosts in ("", "   ", "*", "example.com,*"):
        with pytest.raises(ValidationError):
            Settings(**{**kwargs, "allowed_hosts": hosts})


# ---------------------------------------------------------------------------
# JWT: tipos de claims y revocación
# ---------------------------------------------------------------------------
def test_jwt_rejects_claims_with_wrong_types(client):
    cases = [
        {"sub": ["cli_test"]},
        {"sub": ""},
        {"jti": {"no": "hashable"}},
        {"scopes": "profiles:read"},
        {"scopes": [123]},
        {"tenant": ["otro"]},
    ]
    for overrides in cases:
        token = _raw_token(**overrides)
        response = client.get(
            "/api/v1/profiles/nasa",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401, overrides
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_revoked_token_is_rejected(client):
    token, _ = create_access_token("cli_revoke_test", ["profiles:read"])
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/profiles/nasa", headers=headers).status_code == 200

    revoke_token(token)
    response = client.get("/api/v1/profiles/nasa", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def test_verify_secret_rejects_corrupt_hashes_and_extreme_iterations():
    assert verify_secret("x", "pbkdf2_sha256$600000$!!!!$!!!!") is False
    assert verify_secret("x", "pbkdf2_sha256$999999999999$AAAA$AAAA") is False
    assert verify_secret("x", "no-es-un-hash") is False
    assert verify_secret("x", DUMMY_HASH) is False


def test_hash_secret_bounds_iterations():
    with pytest.raises(ValueError):
        hash_secret("x", iterations=10)
    with pytest.raises(ValueError):
        hash_secret("x", iterations=10_000_000)


# ---------------------------------------------------------------------------
# Middleware: cabeceras, request id y body chunked
# ---------------------------------------------------------------------------
def test_security_headers_on_auth_error(client):
    response = client.get("/api/v1/profiles/nasa")
    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_security_headers_on_trusted_host_rejection(client):
    response = client.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_request_id_is_validated_and_propagated(client):
    custom = "req-test-12345678"
    response = client.get("/api/v1/profiles/nasa", headers={"X-Request-ID": custom})
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == custom
    assert response.json()["error"]["request_id"] == custom

    generated = client.get("/api/v1/health", headers={"X-Request-ID": "corto"})
    assert generated.headers["X-Request-ID"].startswith("req_")


def test_chunked_body_without_content_length_is_limited():
    async def inner(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def scenario():
        app = MaxBodySizeMiddleware(inner)
        chunks = [b"x" * 400_000, b"x" * 400_000, b"x" * 400_000]
        pending = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]
        sent: list[dict] = []

        async def receive():
            return pending.pop(0)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "http_version": "1.1",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(scenario())
    status = next(
        message["status"] for message in sent if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert status == 413
    assert b"PAYLOAD_TOO_LARGE" in body


# ---------------------------------------------------------------------------
# Jobs: error genérico, cuota concurrente y aislamiento entre tenants
# ---------------------------------------------------------------------------
def test_job_failure_does_not_leak_internal_details(client, monkeypatch):
    owner = "cli_job_error_" + uuid.uuid4().hex[:8]
    headers = _token(owner, ["jobs:read", "jobs:write"])

    def boom(*args, **kwargs):
        raise RuntimeError("ruta secreta C:\\datos\\password.txt")

    monkeypatch.setattr(jobs_service.scraper, "build_result", boom)

    created = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"type": "profile", "target": {"username": "nasa"}},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    job = _wait_job(client, headers, job_id, {"failed"})
    assert job["error"]["code"] == "JOB_FAILED"
    assert job["error"]["message"] == "El job falló durante la ejecución."
    assert job["error"]["details"] == {}
    assert "password" not in json.dumps(job)
    assert "secreta" not in json.dumps(job)


def test_concurrent_job_quota_is_enforced(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "concurrent_jobs_limit", 1)
    monkeypatch.setattr(settings, "job_simulation_seconds", 30.0)
    monkeypatch.setattr(settings, "job_steps", 100)

    owner = "cli_concurrency_" + uuid.uuid4().hex[:8]
    headers = _token(owner, ["jobs:read", "jobs:write"])
    body = {"type": "profile", "target": {"username": "nasa"}}

    first = client.post("/api/v1/jobs", headers=headers, json=body)
    assert first.status_code == 202, first.text

    second = client.post("/api/v1/jobs", headers=headers, json=body)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "CONCURRENCY_LIMIT"
    assert second.headers["Retry-After"] == "5"

    client.delete(f"/api/v1/jobs/{first.json()['job_id']}", headers=headers)


def test_jobs_are_isolated_between_tenants(client):
    tenant_a = _token("cli_tenant_a_" + uuid.uuid4().hex[:8], ["jobs:read", "jobs:write"])
    tenant_b = _token("cli_tenant_b_" + uuid.uuid4().hex[:8], ["jobs:read", "jobs:write"])

    created = client.post(
        "/api/v1/jobs",
        headers=tenant_a,
        json={"type": "profile", "target": {"username": "nasa"}},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    hidden = client.get(f"/api/v1/jobs/{job_id}", headers=tenant_b)
    assert hidden.status_code == 404

    cancelled = client.delete(f"/api/v1/jobs/{job_id}", headers=tenant_a)
    assert cancelled.status_code == 200


def test_usage_only_counts_current_month(client):
    owner = "cli_usage_" + uuid.uuid4().hex[:8]
    headers = _token(owner, ["account:read"])
    now = datetime.now(timezone.utc)
    previous_month = now.replace(day=1) - timedelta(days=1)

    db = SessionLocal()
    try:
        for suffix, created_at in (("old", previous_month), ("new", now)):
            db.add(
                JobModel(
                    id=f"job_usage_{suffix}_{uuid.uuid4().hex[:8]}",
                    owner=owner,
                    type="profile",
                    target="{}",
                    options="{}",
                    status="completed",
                    items_scraped=1,
                    items_total=1,
                    created_at=created_at,
                )
            )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/v1/account/usage", headers=headers)
    assert response.status_code == 200
    assert response.json()["requests_used"] == 10


# ---------------------------------------------------------------------------
# Cifrado en reposo de URL de webhook y usuario de proxy
# ---------------------------------------------------------------------------
def test_webhook_url_is_encrypted_at_rest(client, auth_headers):
    marker = "token-super-secreto-" + uuid.uuid4().hex[:8]
    created = client.post(
        "/api/v1/webhooks",
        headers=auth_headers,
        json={
            "url": f"https://example.com/hook?token={marker}",
            "events": ["job.completed"],
        },
    )
    assert created.status_code == 201, created.text
    webhook_id = created.json()["webhook_id"]

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT url FROM webhooks WHERE id = :id"), {"id": webhook_id}
        ).scalar_one()
    assert stored.startswith("gAAAAA")
    assert marker not in stored

    listed = client.get("/api/v1/webhooks", headers=auth_headers)
    urls = [item["url"] for item in listed.json()["data"]]
    assert any(marker in url for url in urls)


def test_proxy_username_is_encrypted_and_credentials_are_split(client, admin_headers):
    marker = "proxy-user-" + uuid.uuid4().hex[:8]
    created = client.post(
        "/api/v1/admin/proxies",
        headers=admin_headers,
        json={
            "url": f"http://{marker}:clave-secreta@8.8.8.8:8080",
            "country": "US",
        },
    )
    assert created.status_code == 201, created.text
    proxy_id = created.json()["proxy_id"]
    assert created.json()["url"].rstrip("/") == "http://8.8.8.8:8080"

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT username, password FROM proxies WHERE id = :id"),
            {"id": proxy_id},
        ).one()
    assert row.username.startswith("gAAAAA")
    assert marker not in row.username
    assert row.password.startswith("gAAAAA")
    assert "clave-secreta" not in row.password


def test_migration_encrypts_legacy_plaintext_values(client):
    import importlib.util
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "migrate_secrets.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_secrets", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    suffix = uuid.uuid4().hex[:8]
    proxy_id = f"proxy_legacy_{suffix}"
    webhook_id = f"wh_legacy_{suffix}"
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO proxies "
                "(id, owner, url, username, password, country, status, "
                " last_checked_at, created_at) "
                "VALUES (:id, 'legacy', 'http://8.8.8.8:8080', "
                "'legacy-user', 'legacy-pass', NULL, 'unknown', NULL, :now)"
            ),
            {"id": proxy_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO webhooks "
                "(id, owner, url, events, secret, active, created_at) "
                "VALUES (:id, 'legacy', 'https://example.com/legacy?token=plano', "
                "'[]', NULL, 1, :now)"
            ),
            {"id": webhook_id, "now": now},
        )

    db = SessionLocal()
    try:
        module.migrate(db)
    finally:
        db.close()

    with engine.connect() as connection:
        proxy = connection.execute(
            text("SELECT username, password FROM proxies WHERE id = :id"),
            {"id": proxy_id},
        ).one()
        webhook = connection.execute(
            text("SELECT url FROM webhooks WHERE id = :id"), {"id": webhook_id}
        ).scalar_one()

    assert proxy.username.startswith("gAAAAA")
    assert proxy.password.startswith("gAAAAA")
    assert webhook.startswith("gAAAAA")
    assert "legacy-user" not in proxy.username
    assert "legacy-pass" not in proxy.password
    assert "plano" not in webhook


def test_session_cannot_reference_another_tenants_proxy(client, admin_headers):
    own = client.post(
        "/api/v1/admin/proxies",
        headers=admin_headers,
        json={"url": "http://8.8.8.8:8080"},
    )
    assert own.status_code == 201, own.text

    foreign_id = "proxy_foreign_" + uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        db.add(
            ProxyModel(
                id=foreign_id,
                owner="otro_tenant",
                url="http://8.8.8.8:8080",
                username=None,
                password=None,
                country=None,
                status="unknown",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    cookies = json.dumps([{"name": "sessionid", "value": "x"}])
    rejected = client.post(
        "/api/v1/admin/sessions",
        headers=admin_headers,
        json={"username": "cuenta", "cookies": cookies, "proxy_id": foreign_id},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["details"]["field"] == "proxy_id"

    accepted = client.post(
        "/api/v1/admin/sessions",
        headers=admin_headers,
        json={
            "username": "cuenta",
            "cookies": cookies,
            "proxy_id": own.json()["proxy_id"],
        },
    )
    assert accepted.status_code == 201, accepted.text


# ---------------------------------------------------------------------------
# SSRF: IPv4 mapeada en IPv6
# ---------------------------------------------------------------------------
def test_ssrf_blocks_ipv4_mapped_ipv6(client, auth_headers):
    for url in (
        "http://[::ffff:127.0.0.1]/hook",
        "http://[::ffff:169.254.169.254]/hook",
    ):
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": url, "events": ["job.completed"]},
        )
        assert response.status_code == 422, url


# ---------------------------------------------------------------------------
# Documentación y límites de longitud
# ---------------------------------------------------------------------------
def test_docs_are_disabled_when_debug_is_false(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert "docs" not in client.get("/").json()


def test_input_max_lengths_are_enforced(client, auth_headers):
    too_long_id = client.post(
        "/api/v1/auth/token",
        json={"client_id": "x" * 129, "client_secret": "x"},
    )
    assert too_long_id.status_code == 422

    too_long_secret = client.post(
        "/api/v1/webhooks",
        headers=auth_headers,
        json={
            "url": "https://example.com/hook",
            "events": ["job.completed"],
            "secret": "x" * 257,
        },
    )
    assert too_long_secret.status_code == 422

    too_long_hashtag = client.get(
        "/api/v1/hashtags/" + "a" * 101, headers=auth_headers
    )
    assert too_long_hashtag.status_code == 422


def test_cursor_length_is_bounded(client, auth_headers):
    response = client.get(
        "/api/v1/jobs?cursor=" + "A" * 513, headers=auth_headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_payload_too_large_keeps_rate_limit_and_security_headers(
    client, auth_headers
):
    response = client.post(
        "/api/v1/webhooks",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b"x" * 1_100_000,
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert "X-RateLimit-Limit" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"
