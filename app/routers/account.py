"""Endpoints de consumo y límites de la cuenta."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_client, require_scope
from ..models import Job
from ..schemas import Limits, Usage

router = APIRouter(
    prefix="/account",
    tags=["Account"],
    dependencies=[Depends(require_scope("account:read"))],
)


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


@router.get("/usage", response_model=Usage, summary="Consultar consumo")
def get_usage(
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
) -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    start, end = _month_bounds(now)

    jobs_this_month = (
        db.query(Job)
        .filter(
            Job.owner == client["sub"],
            Job.created_at >= start,
            Job.created_at < end,
        )
        .count()
    )
    active_jobs = (
        db.query(Job)
        .filter(Job.owner == client["sub"], Job.status.in_(["queued", "running"]))
        .count()
    )

    return {
        "plan": "pro",
        "period": {"start": start, "end": end},
        "requests_used": jobs_this_month * 10,
        "requests_limit": settings.requests_limit,
        "concurrent_jobs": active_jobs,
        "concurrent_jobs_limit": settings.concurrent_jobs_limit,
        "reset_at": end,
    }


@router.get("/limits", response_model=Limits, summary="Consultar límites")
def get_limits() -> dict:
    return {
        "plan": "pro",
        "limits": {
            "profiles": {"requests_per_minute": 60, "requests_per_day": 10_000},
            "jobs": {"requests_per_minute": 10, "requests_per_day": 2_000},
            "search": {"requests_per_minute": 30, "requests_per_day": 5_000},
        },
    }
