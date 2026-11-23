"""Endpoints de webhooks."""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session

from ..audit import log_event
from ..database import get_db
from ..dependencies import require_scope
from ..errors import APIError
from ..models import Webhook as WebhookModel
from ..pagination import paginate
from ..schemas import Webhook, WebhookCreateRequest, WebhookCreated, WebhookList
from ..services import webhooks as webhooks_service

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _serialize(hook: WebhookModel) -> dict:
    return {
        "webhook_id": hook.id,
        "url": hook.url,
        "events": json.loads(hook.events or "[]"),
        "active": hook.active,
        "created_at": hook.created_at,
    }


@router.post(
    "",
    response_model=WebhookCreated,
    status_code=201,
    summary="Registrar un webhook",
    description=(
        "El campo `secret` se devuelve solo en esta respuesta: es la clave HMAC "
        "con la que se firman las entregas y no se puede recuperar después."
    ),
)
def create_webhook(
    body: WebhookCreateRequest,
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("webhooks:write")),
) -> dict:
    # Si el cliente no aporta secreto se genera uno; así la entrega siempre va
    # firmada sin exigir un campo nuevo en el contrato de entrada.
    secret = body.secret or webhooks_service.generate_secret()
    hook = WebhookModel(
        id="wh_" + uuid.uuid4().hex[:12],
        owner=client["sub"],
        url=str(body.url),
        events=json.dumps([event.value for event in body.events]),
        secret=secret,
        active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    log_event("webhook.created", actor=client["sub"], webhook_id=hook.id)
    return {**_serialize(hook), "secret": secret}


@router.get("", response_model=WebhookList, summary="Listar webhooks")
def list_webhooks(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("webhooks:read")),
) -> dict:
    hooks = (
        db.query(WebhookModel)
        .filter(WebhookModel.owner == client["sub"])
        .order_by(WebhookModel.created_at.desc())
        .all()
    )
    chunk, pagination = paginate(hooks, limit, cursor)
    return {"data": [_serialize(hook) for hook in chunk], "pagination": pagination}


@router.delete(
    "/{webhook_id}",
    status_code=204,
    summary="Eliminar un webhook",
)
def delete_webhook(
    webhook_id: str = Path(..., pattern=r"^wh_[A-Za-z0-9]+$"),
    db: Session = Depends(get_db),
    client: dict = Depends(require_scope("webhooks:write")),
):
    hook = db.get(WebhookModel, webhook_id)
    if hook is None or hook.owner != client["sub"]:
        raise APIError(404, "NOT_FOUND", "El webhook no existe.")
    db.delete(hook)
    db.commit()
    log_event("webhook.deleted", actor=client["sub"], webhook_id=webhook_id)
    return Response(status_code=204)
