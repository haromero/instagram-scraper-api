"""Regresiones del feedback loop: Docker, Render y RapidAPI.

Estas pruebas validan los artefactos de despliegue sin necesidad de tener
Docker instalado. Primero fallan; después se corrige la implementación.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_is_multistage_nonroot_and_healthchecked():
    """La imagen debe ser multi-stage, no root y con healthcheck real."""
    text = _read("Dockerfile")

    assert re.search(r"^FROM\s+python:3\.12-slim\s+AS\s+builder", text, re.I | re.M)
    assert re.search(r"^FROM\s+python:3\.12-slim\s+AS\s+runtime", text, re.I | re.M)
    assert re.search(r"^USER\s+(?!root\b)\w+", text, re.M)
    assert "HEALTHCHECK" in text
    assert "/api/v1/health" in text
    assert "gunicorn" in text
    assert "${PORT:-8000}" in text
    assert "EXPOSE 8000" in text
    assert "COPY .env" not in text
    assert "ADD .env" not in text


def test_dockerignore_excludes_secrets_and_local_artifacts():
    """Nunca se copian secretos ni artefactos locales al contexto de build."""
    lines = [
        line.strip().rstrip("/")
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for entry in (
        ".env",
        ".venv",
        "*.db",
        "*.db-shm",
        "*.db-wal",
        "__pycache__",
        ".pytest_cache",
        ".git",
    ):
        assert entry in lines, f"falta {entry} en .dockerignore"


def test_compose_interpolation_file_is_intentionally_empty():
    """`.env.compose` evita interpolar los hashes PBKDF2 que contienen '$'."""
    lines = [
        line.strip()
        for line in _read(".env.compose").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == []


def test_compose_wires_api_redis_and_loopback_ports():
    """El compose debe arrancar la API con Redis sin exponerlo a la LAN."""
    compose = yaml.safe_load(_read("docker-compose.yml"))
    services = compose["services"]

    assert "api" in services and "redis" in services
    api = services["api"]
    assert api["build"] == "."
    # Los hashes PBKDF2 llevan "$": raw evita que Compose los interpole.
    assert api["env_file"] == [{"path": ".env", "format": "raw"}]
    assert api["environment"]["IG_RATE_LIMIT_BACKEND"] == "redis"
    assert api["environment"]["IG_REDIS_URL"] == "redis://redis:6379/0"
    assert api["depends_on"]["redis"]["condition"] == "service_healthy"
    assert any("127.0.0.1:8000:8000" in port for port in api["ports"])

    redis = services["redis"]
    assert redis["ports"] == ["127.0.0.1:6379:6379"]


def test_render_blueprint_is_deployable():
    """El blueprint debe usar Docker, healthcheck, Postgres y Key Value."""
    blueprint = yaml.safe_load(_read("render.yaml"))
    web = next(item for item in blueprint["services"] if item["type"] == "web")
    keyvalue = next(
        item for item in blueprint["services"] if item["type"] == "keyvalue"
    )
    database = blueprint["databases"][0]
    env = {item["key"]: item for item in web["envVars"]}

    assert web["runtime"] == "docker"
    assert web["healthCheckPath"] == "/api/v1/health"
    assert web["plan"] == "free"
    assert keyvalue["ipAllowList"] == []

    assert env["IG_JWT_SECRET"]["generateValue"] is True
    # Un valor generado no es una clave Fernet válida: hay que pedirlo.
    assert env["IG_ENCRYPTION_KEY"]["sync"] is False
    assert "generateValue" not in env["IG_ENCRYPTION_KEY"]
    assert env["IG_CLIENT_SECRET_HASH"]["sync"] is False

    assert env["IG_DATABASE_URL"]["fromDatabase"] == {
        "name": database["name"],
        "property": "connectionString",
    }
    assert env["IG_REDIS_URL"]["fromService"] == {
        "type": "keyvalue",
        "name": keyvalue["name"],
        "property": "connectionString",
    }
    assert env["IG_RATE_LIMIT_BACKEND"]["value"] == "redis"
    assert env["IG_SCRAPER_BACKEND"]["value"] == "bluesky"
    assert str(env["IG_OPENAPI_ENABLED"]["value"]).lower() == "true"
    # Render termina TLS; forzar HTTPS dentro del contenedor rompería el healthcheck.
    assert str(env["IG_FORCE_HTTPS"]["value"]).lower() != "true"
    assert "*.onrender.com" in env["IG_ALLOWED_HOSTS"]["value"]


def test_requirements_include_deployment_dependencies():
    """Gunicorn, psycopg3 y PyYAML son necesarios en la imagen."""
    requirements = _read("requirements.txt").lower()
    for dependency in ("gunicorn", "psycopg", "pyyaml", "uvicorn"):
        assert dependency in requirements


def test_postgres_url_uses_psycopg3_driver():
    """Render entrega postgres:// o postgresql://; SQLAlchemy necesita psycopg3."""
    from app.database import normalize_database_url

    assert normalize_database_url("postgres://u:p@host/db") == (
        "postgresql+psycopg://u:p@host/db"
    )
    assert normalize_database_url("postgresql://u:p@host/db") == (
        "postgresql+psycopg://u:p@host/db"
    )
    assert normalize_database_url("postgresql+psycopg://u:p@host/db") == (
        "postgresql+psycopg://u:p@host/db"
    )
    assert normalize_database_url("sqlite:///./igscraper.db") == (
        "sqlite:///./igscraper.db"
    )


def test_openapi_can_be_published_without_enabling_docs():
    """RapidAPI necesita /openapi.json sin exponer /docs en producción."""
    config = _read("app/config.py")
    main = _read("app/main.py")

    assert "openapi_enabled: bool = False" in config
    assert "settings.openapi_enabled" in main
    assert 'openapi_url="/openapi.json" if settings.openapi_enabled else None' in main
    assert 'docs_url="/docs" if _docs_enabled else None' in main


def test_readme_documents_both_deployment_targets():
    """La guía debe cubrir Render y el paso posterior en RapidAPI."""
    readme = _read("README.md")
    for marker in (
        "render.yaml",
        "RapidAPI",
        "docker compose --env-file .env.compose up --build",
    ):
        assert marker in readme


def test_generate_secrets_produces_verifiable_values():
    """El generador de secretos debe emitir hashes que validan su secreto."""
    from app.passwords import verify_secret

    env = os.environ.copy()
    env["DISABLE_SQLALCHEMY_CEXT"] = "1"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_secrets.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1)
        for line in result.stdout.strip().splitlines()
        if "=" in line
    )
    assert set(values) == {
        "IG_ENCRYPTION_KEY",
        "IG_JWT_SECRET",
        "CLIENT_SECRET",
        "IG_CLIENT_SECRET_HASH",
        "ADMIN_SECRET",
        "IG_ADMIN_CLIENT_SECRET_HASH",
    }
    assert verify_secret(values["CLIENT_SECRET"], values["IG_CLIENT_SECRET_HASH"])
    assert verify_secret(values["ADMIN_SECRET"], values["IG_ADMIN_CLIENT_SECRET_HASH"])
    assert len(values["IG_JWT_SECRET"]) >= 32


def test_openapi_is_disabled_by_default(client):
    """El contrato no se publica por defecto en producción."""
    assert client.get("/openapi.json").status_code == 404


def test_openapi_can_be_enabled_without_docs():
    """Con IG_OPENAPI_ENABLED=true se publica el spec, pero no la UI."""
    env = os.environ.copy()
    env["IG_OPENAPI_ENABLED"] = "true"
    code = (
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c=TestClient(app);"
        "print(c.get('/openapi.json').status_code, c.get('/docs').status_code)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "200 404" in result.stdout, result.stdout
