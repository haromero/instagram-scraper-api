"""Endpoint de búsqueda de usuarios y hashtags."""

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..dependencies import require_scope
from ..pagination import paginate
from ..schemas import SearchResultList
from ..services import scraper

router = APIRouter(
    prefix="/search",
    tags=["Search"],
    dependencies=[Depends(require_scope("search:read"))],
)


@router.get("", response_model=SearchResultList, summary="Buscar usuarios y hashtags")
def search(
    q: str = Query(..., min_length=1, max_length=100),
    kind: Literal["user", "hashtag", "all"] = Query(default="all", alias="type"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    results = scraper.search(q, kind=kind, count=120)
    chunk, pagination = paginate(results, limit, cursor)
    return {"data": chunk, "pagination": pagination}
