"""Tests de regresión de los hallazgos de la auditoría OWASP API Top 10."""

import json
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import text

from app.config import get_settings
from app.database import engine
from app.security import create_access_token


def _token(sub: str, scopes: list[str]) -> dict[str, str]:
    token, _ = create_access_token(sub, scopes)
    return {"Authorization": f"Bearer {token}"}


def test_login_ok_and_error_envelope(client, auth_headers):
    response = client.post(
        "/api/v1/auth/token",
        json={"client_id": "cli_test", "client_secret": "test-secret-123456"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "admin:write" not in body["scopes"]
    assert body["token_type"] == "Bearer"


def test_login_failure_has_www_authenticate(client):
    response = client.post(
        "/api/v1/auth/token",
        json={"client_id": "cli_test", "client_secret": "incorrecto"},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "INVALID_CLIENT"


def test_normal_token_cannot_access_admin(client, auth_headers):
    response = client.get("/api/v1/admin/sessions", headers=auth_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_admin_token_can_access_admin(client, admin_headers):
    response = client.get("/api/v1/admin/sessions", headers=admin_headers)
    assert response.status_code == 200


def test_write_scope_is_enforced_on_jobs(client):
    headers = _token("cli_readonly", ["profiles:read", "jobs:read"])
    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"type": "profile", "target": {"username": "nasa"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_scope"] == "jobs:write"


def test_ssrf_is_blocked_in_webhooks(client, auth_headers):
    for url in (
        "http://127.0.0.1:8000/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://localtest.me/hook",
    ):
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": url, "events": ["job.completed"]},
        )
        assert response.status_code == 422, response.text


def test_body_size_limit(client, auth_headers):
    response = client.post(
        "/api/v1/webhooks",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b"x" * 1_100_000,
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_jwt_requires_exp_and_audience(client):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    no_exp = jwt.encode(
        {
            "sub": "cli_test",
            "scopes": ["profiles:read"],
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now,
            "nbf": now,
            "jti": "x",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get(
        "/api/v1/profiles/nasa", headers={"Authorization": f"Bearer {no_exp}"}
    )
    assert response.status_code == 401

    wrong_aud = jwt.encode(
        {
            "sub": "cli_test",
            "scopes": ["profiles:read"],
            "iss": settings.jwt_issuer,
            "aud": "otra-api",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": "y",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get(
        "/api/v1/profiles/nasa", headers={"Authorization": f"Bearer {wrong_aud}"}
    )
    assert response.status_code == 401


def test_invalid_cursor(client, auth_headers):
    response = client.get(
        "/api/v1/jobs?cursor=no-es-base64", headers=auth_headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_secrets_are_encrypted_at_rest(client, admin_headers):
    cookies = json.dumps([{"name": "sessionid", "value": "super-secreto"}])
    response = client.post(
        "/api/v1/admin/sessions",
        headers=admin_headers,
        json={"username": "cuenta_prueba", "cookies": cookies},
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT cookies FROM sessions WHERE id = :id"), {"id": session_id}
        ).scalar_one()

    assert stored.startswith("gAAAAA")
    assert "super-secreto" not in stored


def test_webhooks_are_isolated_between_tenants(client):
    tenant_a = _token("tenant_a", ["webhooks:read", "webhooks:write"])
    tenant_b = _token("tenant_b", ["webhooks:read", "webhooks:write"])

    created = client.post(
        "/api/v1/webhooks",
        headers=tenant_a,
        json={"url": "https://example.com/hook", "events": ["job.completed"]},
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/webhooks", headers=tenant_b)
    assert listed.status_code == 200
    assert listed.json()["data"] == []

    deleted = client.delete(
        f"/api/v1/webhooks/{created.json()['webhook_id']}", headers=tenant_b
    )
    assert deleted.status_code == 404
