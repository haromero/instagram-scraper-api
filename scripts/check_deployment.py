r"""Comprobación rápida de los artefactos de despliegue (sin Docker).

Uso:
    .\.venv\Scripts\python.exe scripts\check_deployment.py

Valida el Dockerfile, .dockerignore, docker-compose.yml, render.yaml y las
dependencias necesarias para desplegar en Render y publicar el spec en RapidAPI.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}" + (f": {detail}" if detail else ""))
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label}" + (f": {detail}" if detail else ""))


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def main() -> int:
    for name in (
        "Dockerfile",
        ".dockerignore",
        ".env.compose",
        "docker-compose.yml",
        "render.yaml",
        "requirements.txt",
    ):
        check(f"archivo {name}", (ROOT / name).is_file())

    dockerfile = read("Dockerfile")
    check(
        "Dockerfile multi-stage",
        bool(
            re.search(r"^FROM\s+python:3\.12-slim\s+AS\s+builder", dockerfile, re.I | re.M)
            and re.search(r"^FROM\s+python:3\.12-slim\s+AS\s+runtime", dockerfile, re.I | re.M)
        ),
    )
    check(
        "Dockerfile usuario no root",
        bool(re.search(r"^USER\s+(?!root\b)\w+", dockerfile, re.M)),
    )
    check(
        "Dockerfile healthcheck real",
        "HEALTHCHECK" in dockerfile and "/api/v1/health" in dockerfile,
    )
    check(
        "Dockerfile respeta PORT con gunicorn",
        "gunicorn" in dockerfile and "${PORT:-8000}" in dockerfile,
    )
    check(
        "Dockerfile no copia secretos",
        "COPY .env" not in dockerfile and "ADD .env" not in dockerfile,
    )

    ignored = {
        line.strip().rstrip("/")
        for line in read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    check(
        ".dockerignore excluye secretos y artefactos",
        {".env", ".venv", "*.db", "__pycache__", ".pytest_cache", ".git"} <= ignored,
        ", ".join(sorted({".env", ".venv", "*.db", "__pycache__", ".pytest_cache", ".git"} - ignored)) or "completo",
    )

    interpolation_lines = [
        line.strip()
        for line in read(".env.compose").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    check(".env.compose no interpola secretos", interpolation_lines == [])

    compose = yaml.safe_load(read("docker-compose.yml"))["services"]
    api = compose.get("api", {})
    redis = compose.get("redis", {})
    # raw conserva los hashes PBKDF2: contienen "$" y Compose los interpoló antes.
    raw_env_file = [{"path": ".env", "format": "raw"}]
    check(
        "compose levanta API con Redis",
        api.get("build") == "."
        and api.get("env_file") == raw_env_file
        and api.get("environment", {}).get("IG_REDIS_URL") == "redis://redis:6379/0",
    )
    check(
        "Redis solo en loopback",
        redis.get("ports") == ["127.0.0.1:6379:6379"],
    )
    worker = compose.get("worker", {})
    check(
        "compose levanta worker real",
        worker.get("command") == ["python", "-m", "app.worker"]
        and worker.get("env_file") == raw_env_file
        and worker.get("environment", {}).get("IG_JOB_BACKEND") == "redis"
        and api.get("environment", {}).get("IG_JOB_BACKEND") == "redis",
    )

    blueprint = yaml.safe_load(read("render.yaml"))
    web = next(item for item in blueprint["services"] if item["type"] == "web")
    keyvalue = next(item for item in blueprint["services"] if item["type"] == "keyvalue")
    database = blueprint["databases"][0]
    env = {item["key"]: item for item in web["envVars"]}
    check("Render usa Docker + healthcheck", web.get("runtime") == "docker" and web.get("healthCheckPath") == "/api/v1/health")
    check(
        "Render vincula PostgreSQL y Redis",
        env["IG_DATABASE_URL"].get("fromDatabase", {}).get("name") == database["name"]
        and env["IG_REDIS_URL"].get("fromService", {}).get("name") == keyvalue["name"],
    )
    check(
        "Secretos Render no se inventan",
        env["IG_JWT_SECRET"].get("generateValue") is True
        and env["IG_ENCRYPTION_KEY"].get("sync") is False
        and env["IG_CLIENT_SECRET_HASH"].get("sync") is False,
    )
    check(
        "OpenAPI publicado sin forzar HTTPS interno",
        str(env["IG_OPENAPI_ENABLED"].get("value")).lower() == "true"
        and str(env["IG_FORCE_HTTPS"].get("value")).lower() != "true",
    )
    check(
        "Render arranca web + worker en el mismo plan",
        web.get("dockerCommand") == "python -m app.launch"
        and env["IG_JOB_BACKEND"].get("value") == "redis",
    )

    for name in (
        "alembic.ini",
        "alembic/env.py",
        "alembic/versions/0001_initial.py",
        "alembic/versions/0002_webhook_deliveries.py",
        "app/db_migrations.py",
        "app/queue.py",
        "app/worker.py",
        "app/launch.py",
        "app/services/webhooks.py",
    ):
        check(f"archivo {name}", (ROOT / name).is_file())

    readme = read("README.md")
    check(
        "webhooks firmados documentados",
        "X-Signature" in readme
        and "webhook_deliveries" in readme
        and "aún no está implementada" not in readme,
    )

    requirements = read("requirements.txt").lower()
    check(
        "dependencias de despliegue",
        all(
            dep in requirements
            for dep in ("gunicorn", "psycopg", "pyyaml", "uvicorn", "alembic")
        ),
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} comprobaciones fallaron.")
        return 1
    print("\nTodas las comprobaciones de despliegue pasaron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
