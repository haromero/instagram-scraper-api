"""Endpoints internos de administración: sesiones y proxies."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session as DBSession

from ..audit import log_event
from ..database import get_db
from ..dependencies import has_scope, require_scope
from ..errors import APIError
from ..models import Proxy as ProxyModel
from ..models import Session as SessionModel
from ..pagination import paginate
from ..schemas import (
    Proxy,
    ProxyCreateRequest,
    ProxyList,
    Session,
    SessionCreateRequest,
    SessionList,
)
from ..validators import split_proxy_credentials

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_scope("admin:write"))],
)


def _serialize_session(session: SessionModel) -> dict:
    return {
        "session_id": session.id,
        "username": session.username,
        "status": session.status,
        "created_at": session.created_at,
        "last_used_at": session.last_used_at,
        "expires_at": session.expires_at,
    }


def _serialize_proxy(proxy: ProxyModel) -> dict:
    # La URL se guarda sin userinfo; nunca devolvemos credenciales.
    return {
        "proxy_id": proxy.id,
        "url": proxy.url,
        "status": proxy.status,
        "country": proxy.country,
        "last_checked_at": proxy.last_checked_at,
    }


def _tenant_filter(query, model, client: dict):
    if has_scope(client, "admin:write"):
        return query
    return query.filter(model.owner == client.get("tenant", client["sub"]))


@router.post(
    "/sessions",
    response_model=Session,
    status_code=201,
    summary="Cargar sesión de Instagram",
)
def create_session(
    body: SessionCreateRequest,
    db: DBSession = Depends(get_db),
    client: dict = Depends(require_scope("admin:write")),
) -> dict:
    try:
        parsed = json.loads(body.cookies)
        if not isinstance(parsed, list):
            raise ValueError("cookies debe ser una lista")
    except Exception:
        raise APIError(
            422,
            "VALIDATION_ERROR",
            "El campo 'cookies' debe ser un JSON de lista válido.",
            {"field": "cookies", "reason": "invalid"},
        )

    if body.proxy_id is not None:
        proxy = db.get(ProxyModel, body.proxy_id)
        if proxy is None or proxy.owner != client["sub"]:
            raise APIError(
                422,
                "VALIDATION_ERROR",
                "El proxy indicado no existe o no pertenece a este cliente.",
                {"field": "proxy_id", "reason": "not_found"},
            )

    now = datetime.now(timezone.utc)
    session = SessionModel(
        id="sess_" + uuid.uuid4().hex[:12],
        owner=client["sub"],
        username=body.username,
        cookies=body.cookies,
        proxy_id=body.proxy_id,
        status="valid",
        created_at=now,
        last_used_at=None,
        expires_at=now + timedelta(days=30),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    log_event("session.created", actor=client["sub"], session_id=session.id)
    return _serialize_session(session)


@router.get("/sessions", response_model=SessionList, summary="Listar sesiones")
def list_sessions(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    db: DBSession = Depends(get_db),
    client: dict = Depends(require_scope("admin:write")),
) -> dict:
    query = _tenant_filter(db.query(SessionModel), SessionModel, client)
    sessions = query.order_by(SessionModel.created_at.desc()).all()
    chunk, pagination = paginate(sessions, limit, cursor)
    return {
        "data": [_serialize_session(session) for session in chunk],
        "pagination": pagination,
    }


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    summary="Revocar una sesión",
)
def delete_session(
    session_id: str = Path(..., pattern=r"^sess_[A-Za-z0-9]+$"),
    db: DBSession = Depends(get_db),
    client: dict = Depends(require_scope("admin:write")),
):
    session = db.get(SessionModel, session_id)
    if session is None or not _can_access(session, client):
        raise APIError(404, "NOT_FOUND", "La sesión no existe.")
    db.delete(session)
    db.commit()
    log_event("session.deleted", actor=client["sub"], session_id=session_id)
    return Response(status_code=204)


@router.post(
    "/proxies",
    response_model=Proxy,
    status_code=201,
    summary="Añadir un proxy",
)
def create_proxy(
    body: ProxyCreateRequest,
    db: DBSession = Depends(get_db),
    client: dict = Depends(require_scope("admin:write")),
) -> dict:
    clean_url, url_username, url_password = split_proxy_credentials(str(body.url))
    proxy = ProxyModel(
        id="proxy_" + uuid.uuid4().hex[:12],
        owner=client["sub"],
        url=clean_url,
        username=body.username or url_username,
        password=body.password or url_password,
        country=body.country,
        status="unknown",
        last_checked_at=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(proxy)
    db.commit()
    db.refresh(proxy)
    log_event("proxy.created", actor=client["sub"], proxy_id=proxy.id)
    return _serialize_proxy(proxy)


@router.get("/proxies", response_model=ProxyList, summary="Listar proxies")
def list_proxies(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    db: DBSession = Depends(get_db),
    client: dict = Depends(require_scope("admin:write")),
) -> dict:
    query = _tenant_filter(db.query(ProxyModel), ProxyModel, client)
    proxies = query.order_by(ProxyModel.created_at.desc()).all()
    chunk, pagination = paginate(proxies, limit, cursor)
    return {
        "data": [_serialize_proxy(proxy) for proxy in chunk],
        "pagination": pagination,
    }


def _can_access(resource, client: dict) -> bool:
    if has_scope(client, "admin:write"):
        return True
    return getattr(resource, "owner", None) == client.get("tenant", client["sub"])
