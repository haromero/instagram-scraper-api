"""Configuración de tests: entorno aislado y cliente HTTP."""

import base64
import hashlib
import os
import secrets
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

TEST_CLIENT_ID = "cli_test"
TEST_CLIENT_SECRET = "test-secret-123456"
TEST_ADMIN_ID = "cli_admin_test"
TEST_ADMIN_SECRET = "admin-secret-123456"


def _hash(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 600_000)
    return (
        f"pbkdf2_sha256$600000${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(digest).decode()}"
    )


def _configure_env() -> None:
    db_path = Path(__file__).resolve().parents[1] / "test_igscraper.db"
    db_path.unlink(missing_ok=True)

    os.environ.update(
        {
            "IG_DATABASE_URL": "sqlite:///./test_igscraper.db",
            "IG_DEBUG": "false",
            "IG_JWT_SECRET": secrets.token_urlsafe(48),
            "IG_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "IG_CLIENT_ID": TEST_CLIENT_ID,
            "IG_CLIENT_SECRET_HASH": _hash(TEST_CLIENT_SECRET),
            "IG_ADMIN_CLIENT_ID": TEST_ADMIN_ID,
            "IG_ADMIN_CLIENT_SECRET_HASH": _hash(TEST_ADMIN_SECRET),
            "IG_RATE_LIMIT_PER_MINUTE": "10000",
            "IG_AUTH_RATE_LIMIT_PER_MINUTE": "10000",
            "IG_JOBS_RATE_LIMIT_PER_MINUTE": "10000",
            "IG_ALLOWED_HOSTS": "testserver,127.0.0.1,localhost",
        }
    )


_configure_env()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, client_id: str, client_secret: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token",
        json={"client_id": client_id, "client_secret": client_secret},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def auth_headers(client):
    return _login(client, TEST_CLIENT_ID, TEST_CLIENT_SECRET)


@pytest.fixture(scope="session")
def admin_headers(client):
    return _login(client, TEST_ADMIN_ID, TEST_ADMIN_SECRET)
