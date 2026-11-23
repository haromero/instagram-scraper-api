"""Endpoints de salud (sin autenticación)."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..errors import APIError
from ..models import Proxy
from ..redis_backend import get_client
from ..schemas import Health, HealthReady
from ..services import jobs as jobs_service

router = APIRouter(tags=["Health"])


def _redis_status() -> str | None:
    """Devuelve ``ok``/``down``, o ``None`` si Redis no está configurado."""
    client = get_client()
    if client is None:
        return None
    try:
        client.ping()
        return "ok"
    except Exception:
        return "down"


@router.get("/health", response_model=Health, summary="Liveness")
def health() -> dict:
    return {"status": "ok", "version": get_settings().version}


@router.get("/health/ready", response_model=HealthReady, summary="Readiness")
def health_ready(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise APIError(
            503,
            "SERVICE_UNAVAILABLE",
            "La base de datos no está operativa.",
            {"database": "down"},
        )

    pool = jobs_service.pool_status()
    queue = (
        "down"
        if pool["shutdown"]
        else "degraded" if pool["active"] >= pool["max_workers"] else "ok"
    )
    total_proxies = db.query(func.count(Proxy.id)).scalar() or 0
    healthy_proxies = (
        db.query(func.count(Proxy.id)).filter(Proxy.status == "healthy").scalar() or 0
    )
    checks: dict = {
        "database": "ok",
        "queue": queue,
        "queue_backend": pool.get("backend", "thread"),
        "workers": {"active": pool["active"], "idle": pool["idle"]},
        "proxies": {"healthy": healthy_proxies, "total": total_proxies},
    }
    if pool.get("backend") == "redis":
        checks["queue_details"] = {
            "pending": pool.get("pending", 0),
            "stream_length": pool.get("stream_length", 0),
        }

    if queue == "down":
        raise APIError(
            503,
            "SERVICE_UNAVAILABLE",
            "La cola de jobs no está operativa.",
            {"queue": "down"},
        )

    redis_status = _redis_status()
    if redis_status is not None:
        checks["redis"] = redis_status
        if redis_status == "down":
            raise APIError(
                503,
                "SERVICE_UNAVAILABLE",
                "Redis no está operativo.",
                {"redis": "down"},
            )

    return {
        "status": "ok",
        "checks": checks,
        "version": get_settings().version,
    }
