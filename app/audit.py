"""Registro de auditoría de eventos sensibles.

Escribe una línea JSON por evento en el logger ``audit``. En producción se
puede enganchar a un sink central (CloudWatch, Loki, Datadog...) sin tocar los
llamadores.
"""

import json
import logging
from datetime import datetime, timezone

from .context import get_request_id

logger = logging.getLogger("audit")


def log_event(event: str, *, actor: str | None = None, outcome: str = "success", **fields) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "actor": actor,
        "outcome": outcome,
        "request_id": get_request_id(),
        **fields,
    }
    logger.info(json.dumps(record, default=str, ensure_ascii=False))
