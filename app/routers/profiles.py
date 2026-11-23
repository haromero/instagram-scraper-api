"""Endpoints de perfiles (con soporte de force_refresh vía jobs)."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_client, has_scope, require_scope
from ..errors import APIError
from ..models import ProfileCache
from ..pagination import paginate
from ..schemas import (
    HighlightList,
    Job,
    PostList,
    Profile,
    StoryList,
    UserSummaryList,
)
from ..services import jobs as jobs_service
from ..services import scraper

router = APIRouter(
    prefix="/profiles",
    tags=["Profiles"],
    dependencies=[Depends(require_scope("profiles:read"))],
)

_USERNAME_PATTERN = r"^[A-Za-z0-9._:@-]{1,253}$"


def _cache_is_fresh(scraped_at: datetime, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0:
        return False
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - scraped_at).total_seconds()
    # Un timestamp futuro (reloj del backend desincronizado) no puede mantener
    # la entrada fresca de forma indefinida.
    return 0 <= age < ttl_seconds


def _accepted(
    db: Session,
    client: dict,
    job_type: str,
    target: dict,
    options: dict | None = None,
) -> JSONResponse:
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
        job_type=job_type,
        target=target,
        options=options,
    )
    return JSONResponse(
        status_code=202,
        content=jsonable_encoder(jobs_service.serialize_job(job)),
    )


@router.get(
    "/{username}",
    response_model=Profile,
    responses={202: {"model": Job}},
    summary="Obtener perfil",
    description="Devuelve el perfil desde caché. Con force_refresh=true encola un job y responde 202.",
)
def get_profile(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    force_refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
):
    if force_refresh:
        return _accepted(db, client, "profile", {"username": username})

    cached = db.get(ProfileCache, username)
    if cached is not None and _cache_is_fresh(
        cached.scraped_at, get_settings().profile_cache_ttl_seconds
    ):
        return json.loads(cached.data)

    profile = scraper.get_profile(username)
    # El TTL se ancla al momento en que se cachea, no al scraped_at que
    # devuelva el backend (que puede ser viejo o incluso futuro).
    cached_at = datetime.now(timezone.utc)
    if cached is None:
        db.add(
            ProfileCache(
                username=username,
                data=json.dumps(profile, default=str),
                scraped_at=cached_at,
            )
        )
    else:
        cached.data = json.dumps(profile, default=str)
        cached.scraped_at = cached_at
    db.commit()
    return profile


@router.get(
    "/{username}/posts",
    response_model=PostList,
    responses={202: {"model": Job}},
    summary="Listar posts de un perfil",
)
def get_profile_posts(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    force_refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
):
    if force_refresh:
        return _accepted(
            db, client, "profile_posts", {"username": username}, {"limit": limit}
        )

    posts = scraper.get_posts(username, count=120)
    chunk, pagination = paginate(posts, limit, cursor)
    return {"data": chunk, "pagination": pagination}


@router.get(
    "/{username}/followers",
    response_model=UserSummaryList,
    responses={202: {"model": Job}},
    summary="Listar seguidores",
)
def get_profile_followers(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    force_refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
):
    if force_refresh:
        return _accepted(
            db,
            client,
            "profile_followers",
            {"username": username},
            {"limit": limit},
        )

    users = scraper.get_followers(username, count=120)
    chunk, pagination = paginate(users, limit, cursor)
    return {"data": chunk, "pagination": pagination}


@router.get(
    "/{username}/following",
    response_model=UserSummaryList,
    responses={202: {"model": Job}},
    summary="Listar cuentas seguidas",
)
def get_profile_following(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    force_refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    client: dict = Depends(get_current_client),
):
    if force_refresh:
        return _accepted(
            db,
            client,
            "profile_following",
            {"username": username},
            {"limit": limit},
        )

    users = scraper.get_following(username, count=120)
    chunk, pagination = paginate(users, limit, cursor)
    return {"data": chunk, "pagination": pagination}


@router.get(
    "/{username}/stories",
    response_model=StoryList,
    summary="Listar stories activas",
)
def get_profile_stories(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    stories = scraper.get_stories(username, count=10)
    chunk, pagination = paginate(stories, limit, cursor)
    return {"data": chunk, "pagination": pagination}


@router.get(
    "/{username}/highlights",
    response_model=HighlightList,
    summary="Listar highlights",
)
def get_profile_highlights(
    username: str = Path(..., pattern=_USERNAME_PATTERN),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    highlights = scraper.get_highlights(username, count=10)
    chunk, pagination = paginate(highlights, limit, cursor)
    return {"data": chunk, "pagination": pagination}
