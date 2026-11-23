"""Endpoints de publicaciones y sus interacciones."""

from fastapi import APIRouter, Depends, Path, Query

from ..dependencies import require_scope
from ..pagination import paginate
from ..schemas import CommentList, Post, UserSummaryList
from ..services import scraper

router = APIRouter(
    prefix="/posts",
    tags=["Posts"],
    dependencies=[Depends(require_scope("posts:read"))],
)


@router.get("/{shortcode}", response_model=Post, summary="Obtener un post")
def get_post(
    shortcode: str = Path(..., min_length=1, max_length=300),
):
    return scraper.get_post(shortcode)


@router.get(
    "/{shortcode}/comments",
    response_model=CommentList,
    summary="Listar comentarios de un post",
)
def get_comments(
    shortcode: str = Path(..., min_length=1, max_length=300),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    comments = scraper.get_comments(shortcode, count=120)
    chunk, pagination = paginate(comments, limit, cursor)
    return {"data": chunk, "pagination": pagination}


@router.get(
    "/{shortcode}/likes",
    response_model=UserSummaryList,
    summary="Listar usuarios que dieron like",
)
def get_likes(
    shortcode: str = Path(..., min_length=1, max_length=300),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    users = scraper.get_likes(shortcode, count=120)
    chunk, pagination = paginate(users, limit, cursor)
    return {"data": chunk, "pagination": pagination}
