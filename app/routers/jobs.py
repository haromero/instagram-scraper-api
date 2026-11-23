"""Endpoints de jobs asíncronos de scraping."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from ..audit import log_event
from ..database import get_db
from ..dependencies import require_scope
from ..errors import APIError
from ..models import Job
from ..pagination import paginate
from ..schemas import Job as JobSchema
from ..schemas import JobCreateRequest, JobList, JobResult, JobStatus, JobType
from ..services import jobs as jobs_service
from ..services import webhooks as webhooks_service

router = APIRouter(prefix="/jobs", tags=["Jobs"])

_REQUIRED_TARGET = {
    "profile": "username",
    "profile_posts": "username",
    "profile_followers": "username",
    "profile_following": "username",
    "post_comments": "shortcode",
    "hashtag_posts": "hashtag",
    "search": "query",
}


def _get_owned_job(db: Session, job_id: str, owner: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.owner != owner:
        raise APIError(404, "NOT_FOUND", "El job no existe.")
    return job


@router.post(
    "",
    response_model=JobSchema,
    status_code=202,
    summary="Encolar un job de scraping",
    description="Crea un job asíncrono y devuelve 202 con el estado inicial.",
)
def create_job(
    body: JobCreateRequest,
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("jobs:write")),
) -> dict:
    required = _REQUIRED_TARGET[body.type.value]
    if not getattr(body.target, required, None):
        raise APIError(
            422,
            "VALIDATION_ERROR",
            f"El campo 'target.{required}' es obligatorio para el tipo '{body.type.value}'.",
            {"field": f"target.{required}", "reason": "required"},
        )

    job = jobs_service.enqueue(
        db,
        owner=client["sub"],
        job_type=body.type.value,
        target=body.target.model_dump(exclude_none=True),
        options=body.options.model_dump(exclude_none=True),
    )
    log_event("job.created", actor=client["sub"], job_id=job.id, job_type=job.type)
    return jobs_service.serialize_job(job)


@router.get("", response_model=JobList, summary="Listar jobs")
def list_jobs(
    status: JobStatus | None = Query(default=None),
    job_type: JobType | None = Query(default=None, alias="type"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("jobs:read")),
) -> dict:
    query = db.query(Job).filter(Job.owner == client["sub"])
    if status is not None:
        query = query.filter(Job.status == status.value)
    if job_type is not None:
        query = query.filter(Job.type == job_type.value)

    jobs = query.order_by(Job.created_at.desc()).all()
    chunk, pagination = paginate(jobs, limit, cursor)
    return {
        "data": [jobs_service.serialize_job(job) for job in chunk],
        "pagination": pagination,
    }


@router.get(
    "/{job_id}",
    response_model=JobSchema,
    summary="Consultar estado de un job",
)
def get_job(
    job_id: str = Path(..., pattern=r"^job_[A-Za-z0-9]+$"),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("jobs:read")),
) -> dict:
    job = _get_owned_job(db, job_id, client["sub"])
    return jobs_service.serialize_job(job)


@router.get(
    "/{job_id}/result",
    response_model=JobResult,
    summary="Obtener resultado de un job",
    description="Solo disponible cuando el job está en estado completed; si no, devuelve 409.",
)
def get_job_result(
    job_id: str = Path(..., pattern=r"^job_[A-Za-z0-9]+$"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("jobs:read")),
) -> dict:
    job = _get_owned_job(db, job_id, client["sub"])
    if job.status != "completed":
        raise APIError(
            409,
            "JOB_NOT_READY",
            "El job aún no ha terminado.",
            {"status": job.status},
        )

    items = json.loads(job.result or "[]")
    chunk, pagination = paginate(items, limit, cursor)
    return {"job_id": job.id, "data": chunk, "pagination": pagination}


@router.delete(
    "/{job_id}",
    response_model=JobSchema,
    summary="Cancelar un job",
    description="Cancela un job en estado queued o running; si ya terminó devuelve 409.",
)
def cancel_job(
    job_id: str = Path(..., pattern=r"^job_[A-Za-z0-9]+$"),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("jobs:write")),
) -> dict:
    job = _get_owned_job(db, job_id, client["sub"])
    if job.status not in ("queued", "running"):
        raise APIError(
            409,
            "JOB_NOT_CANCELLABLE",
            "El job ya terminó y no puede cancelarse.",
            {"status": job.status},
        )

    job.status = "cancelled"
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    log_event("job.cancelled", actor=client["sub"], job_id=job.id)
    webhooks_service.notify("job.cancelled", job.id)
    return jobs_service.serialize_job(job)
