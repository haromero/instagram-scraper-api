"""Entrega de webhooks firmados con HMAC-SHA256.

Cada evento terminal de un job (`job.completed`, `job.failed`, `job.cancelled`)
se envía a los webhooks activos de su dueño. El cuerpo se firma con el secreto
del webhook sobre ``<timestamp>.<cuerpo crudo>`` para que el receptor pueda
verificar integridad y antigüedad:

    X-Signature: sha256=<hex(hmac_sha256(secret, b"<timestamp>.<body>"))>
    X-Webhook-Timestamp: <unix UTC>

La tabla ``webhook_deliveries`` hace idempotente el despacho (única por
webhook + job + evento) y registra intentos, código de respuesta y resultado.
Un fallo del receptor nunca altera el estado del job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..audit import log_event
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Job, Webhook, WebhookDelivery
from ..validators import assert_safe_outbound_url

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
EVENT_HEADER = "X-Webhook-Event"
DELIVERY_HEADER = "X-Webhook-Delivery"
_SIGNATURE_PREFIX = "sha256="
_USER_AGENT = "instagram-scraper-api/1.2 (webhooks)"


# ---------------------------------------------------------------------------
# Firma
# ---------------------------------------------------------------------------
def generate_secret() -> str:
    """Secreto nuevo para un webhook que no lo proporcionó el cliente."""
    return secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """Devuelve ``sha256=<hex>`` del mensaje ``<timestamp>.<body>``."""
    message = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_signature(
    secret: str,
    timestamp: int | str,
    body: bytes,
    signature: str,
    *,
    max_age_seconds: int = 300,
) -> bool:
    """Verifica la firma en tiempo constante y rechaza firmas muy antiguas.

    ``max_age_seconds=0`` desactiva la comprobación de antigüedad.
    """
    if not secret or not signature or not str(signature).startswith(_SIGNATURE_PREFIX):
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if max_age_seconds and abs(int(time.time()) - sent_at) > max_age_seconds:
        return False
    return hmac.compare_digest(sign(secret, sent_at, body), str(signature))


# ---------------------------------------------------------------------------
# Cuerpo del evento
# ---------------------------------------------------------------------------
def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite guarda fechas naive: se interpretan como UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def build_payload(event: str, job: Job, delivery_id: str) -> bytes:
    """JSON canónico y determinista: la firma debe ser reproducible."""
    payload = {
        "id": delivery_id,
        "event": event,
        "created_at": _iso(datetime.now(timezone.utc)),
        "data": {
            "job_id": job.id,
            "type": job.type,
            "status": job.status,
            "target": json.loads(job.target or "{}"),
            "progress": {
                "items_scraped": job.items_scraped or 0,
                "items_total": job.items_total or 0,
            },
            "result_url": f"/api/v1/jobs/{job.id}/result",
            "error": json.loads(job.error) if job.error else None,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
        },
    }
    return json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")


def _http_client() -> httpx.Client:
    """Cliente HTTP propio del worker; se cierra al terminar el lote."""
    return httpx.Client(
        timeout=get_settings().webhook_timeout_seconds,
        headers={"User-Agent": _USER_AGENT},
    )


# ---------------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------------
def _send(
    db: Session,
    delivery: WebhookDelivery,
    hook: Webhook,
    job: Job,
    event: str,
    client: httpx.Client,
    settings: Settings,
    deadline: float,
) -> None:
    body = build_payload(event, job, delivery.id)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(hook.secret or "", timestamp, body),
        TIMESTAMP_HEADER: str(timestamp),
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery.id,
    }

    attempts = 0
    status = "failed"
    response_code = None
    error = None
    for attempt in range(1, settings.webhook_max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = "Se agotó el presupuesto de tiempo de entrega."
            break
        attempts = attempt
        try:
            # La URL se validó al registrar el webhook; se repite al enviar
            # para cortar DNS rebinding y cambios posteriores del destino.
            assert_safe_outbound_url(hook.url, resolve=settings.validate_outbound_dns)
            response = client.post(
                hook.url,
                content=body,
                headers=headers,
                timeout=min(settings.webhook_timeout_seconds, remaining),
            )
            response_code = response.status_code
            if 200 <= response_code < 300:
                status = "delivered"
                error = None
                break
            error = f"HTTP {response_code}"
        except Exception as exc:  # red, timeout, SSRF...
            error = str(exc)[:500] or type(exc).__name__
        if attempt < settings.webhook_max_attempts:
            wait = min(
                settings.webhook_backoff_seconds * (2 ** (attempt - 1)),
                max(0.0, deadline - time.monotonic()),
            )
            if wait:
                time.sleep(wait)

    delivery.status = status
    delivery.attempts = attempts
    delivery.response_code = response_code
    delivery.error = None if status == "delivered" else error
    if status == "delivered":
        delivery.delivered_at = datetime.now(timezone.utc)
    db.commit()

    log_event(
        "webhook.delivered" if status == "delivered" else "webhook.failed",
        actor=job.owner,
        outcome="success" if status == "delivered" else "failure",
        webhook_id=hook.id,
        delivery_id=delivery.id,
        job_id=job.id,
        event=event,
        attempts=attempts,
        response_code=response_code,
    )


def notify(event: str, job_id: str, *, client: httpx.Client | None = None) -> int:
    """Entrega un evento a todos los webhooks suscritos; devuelve cuántos.

    Se usa desde el pool de hilos y desde el worker. Nunca propaga errores:
    un webhook caído no debe cambiar el estado del job ni tumbar el worker.
    """
    settings = get_settings()
    deadline = time.monotonic() + settings.webhook_delivery_budget_seconds
    owns_client = client is None
    http_client = client or _http_client()
    db = SessionLocal()
    sent = 0
    try:
        job = db.get(Job, job_id)
        if job is None:
            return 0
        hooks = (
            db.query(Webhook)
            .filter(Webhook.owner == job.owner, Webhook.active.is_(True))
            .all()
        )
        for hook in hooks:
            try:
                try:
                    subscribed = json.loads(hook.events or "[]")
                except json.JSONDecodeError:
                    continue
                if event not in subscribed:
                    continue
                already = (
                    db.query(WebhookDelivery)
                    .filter_by(webhook_id=hook.id, job_id=job_id, event=event)
                    .first()
                )
                if already is not None:
                    continue

                delivery = WebhookDelivery(
                    id="whd_" + uuid.uuid4().hex[:16],
                    webhook_id=hook.id,
                    job_id=job_id,
                    event=event,
                    status="pending",
                    attempts=0,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(delivery)
                db.commit()
                sent += 1
                _send(db, delivery, hook, job, event, http_client, settings, deadline)
            except Exception:
                logger.exception(
                    "Falló la entrega del webhook %s para el job %s", hook.id, job_id
                )
                db.rollback()
    except Exception:
        logger.exception("No se pudieron preparar las entregas del job %s", job_id)
    finally:
        db.close()
        if owns_client:
            http_client.close()
    return sent
