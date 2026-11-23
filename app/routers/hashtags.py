"""Endpoints de hashtags."""

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_current_client, has_scope, require_scope
from ..errors import APIError
from ..pagination import paginate
from ..schemas import Hashtag, Job, PostList
from ..services import jobs as jobs_service
from ..services import scraper

router = APIRouter(
    prefix="/hashtags",
    tags=["Hashtags"],
    dependencies=[Depends(require_scope("hashtags:read"))],
)

_HASHTAG_PATTERN = r"^[A-Za-z0-9_]+$"


@router.get("/{hashtag}", response_model=Hashtag, summary="Obtener un hashtag")
def get_hashtag(
    hashtag: str = Path(..., pattern=_HASHTAG_PATTERN, max_length=100),
):
    return scraper.get_hashtag(hashtag)


@router.get(
    "/{hashtag}/posts",
    response_model=PostList,
    responses={202: {"model": Job}},
    summary="Listar posts de un hashtag",
)
def get_hashtag_posts(
    hashtag: str = Path(..., pattern=_HASHTAG_PATTERN, max_length=100),
    mode: Literal["top", "recent"] = Query(default="recent"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    force_refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
):
    if force_refresh:
        if not has_scope(client, "jobs:write"):
            raise APIError(
                403,
                "FORBIDDEN",
                "El token no tiene el scope requerido.",
                {"required_scope": "jobs:write"},
            )
        job = jobs_service.enqueue(
            db,
            owner=client["sub"],
            job_type="hashtag_posts",
            target={"hashtag": hashtag},
            options={"limit": limit, "mode": mode},
        )
        return JSONResponse(
            status_code=202,
            content=jsonable_encoder(jobs_service.serialize_job(job)),
        )

    posts = scraper.get_hashtag_posts(hashtag, mode=mode, count=120)
    chunk, pagination = paginate(posts, limit, cursor)
    return {"data": chunk, "pagination": pagination}
