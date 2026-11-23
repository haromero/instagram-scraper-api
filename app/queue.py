"""Cola durable de jobs basada en Redis Streams.

La API publica en un stream y un proceso worker independiente consume con un
consumer group. ``XAUTOCLAIM`` recupera mensajes cuyo worker murió (visibilidad)
y ``XPENDING`` expone el backlog real en ``/health/ready``. No hay encargados
de pago: solo el Redis que ya usa el rate limit distribuido.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import Settings

if TYPE_CHECKING:  # pragma: no cover - solo para type checkers
    from redis import Redis


@dataclass(frozen=True)
class ClaimedJob:
    message_id: str
    job_id: str
    attempts: int


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisJobQueue:
    """Productor/consumidor at-least-once con reintentos y dead letter."""

    backend = "redis"

    def __init__(
        self,
        client: "Redis",
        settings: Settings,
        *,
        consumer_name: str | None = None,
    ) -> None:
        self._client = client
        self._stream = settings.job_queue_stream
        self._dead_stream = f"{settings.job_queue_stream}:dead"
        self._group = settings.job_consumer_group
        self._consumer = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        self._visibility_ms = int(settings.job_visibility_seconds * 1000)
        self._batch = settings.job_claim_batch
        self._max_workers = settings.concurrent_jobs_limit

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
        except Exception as exc:
            # BUSYGROUP es la respuesta normal cuando el grupo ya existe.
            if "BUSYGROUP" not in str(exc).upper():
                raise

    def publish(self, job_id: str) -> str:
        self._ensure_group()
        message_id = self._client.xadd(
            self._stream, {"job_id": job_id, "attempts": "0"}
        )
        return _text(message_id)

    @staticmethod
    def _entries(payload: Any) -> list[tuple[str, dict[str, Any]]]:
        if not payload:
            return []
        # XAUTOCLAIM devuelve [next_id, entries, ...]; XREADGROUP devuelve
        # [[stream, entries]].
        candidate = payload[1] if len(payload) > 1 else payload
        if candidate and isinstance(candidate[0], list):
            # XREADGROUP: [[stream, [(id, fields)]]]
            candidate = candidate[0][1]
        return [(_text(item[0]), item[1]) for item in candidate]

    def claim(self) -> list[ClaimedJob]:
        """Recupera mensajes atascados y lee mensajes nuevos."""
        self._ensure_group()
        claimed: list[ClaimedJob] = []

        try:
            autoclaimed = self._client.xautoclaim(
                name=self._stream,
                groupname=self._group,
                consumername=self._consumer,
                min_idle_time=self._visibility_ms,
                start_id="0-0",
                count=self._batch,
            )
            for message_id, fields in self._entries(autoclaimed):
                claimed.append(
                    ClaimedJob(
                        message_id=message_id,
                        job_id=_text(fields["job_id"]),
                        attempts=int(_text(fields.get("attempts", "0"))),
                    )
                )
        except Exception as exc:  # Redis < 6.2: se usa XPENDING + XCLAIM
            if "unknown command" not in str(exc).lower():
                raise
            for message_id, _idle, _deliveries in self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=self._batch
            ):
                recovered = self._client.xclaim(
                    self._stream,
                    self._group,
                    self._consumer,
                    self._visibility_ms,
                    [_text(message_id)],
                )
                for recovered_id, fields in self._entries(recovered):
                    claimed.append(
                        ClaimedJob(
                            message_id=recovered_id,
                            job_id=_text(fields["job_id"]),
                            attempts=int(_text(fields.get("attempts", "0"))),
                        )
                    )

        remaining = self._batch - len(claimed)
        if remaining > 0:
            new_messages = self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: ">"},
                count=remaining,
            )
            for message_id, fields in self._entries(new_messages):
                claimed.append(
                    ClaimedJob(
                        message_id=message_id,
                        job_id=_text(fields["job_id"]),
                        attempts=int(_text(fields.get("attempts", "0"))),
                    )
                )
        return claimed

    def ack(self, message_id: str) -> None:
        self._client.xack(self._stream, self._group, message_id)

    def retry(self, message_id: str, job_id: str, attempts: int) -> None:
        """Vuelve a encolar con un intento más y confirma el mensaje anterior."""
        pipeline = self._client.pipeline(transaction=True)
        pipeline.xadd(
            self._stream, {"job_id": job_id, "attempts": str(attempts + 1)}
        )
        pipeline.xack(self._stream, self._group, message_id)
        pipeline.execute()

    def dead_letter(
        self, message_id: str, job_id: str, attempts: int, error: str
    ) -> None:
        """Mueve el mensaje a :dead tras agotar los intentos."""
        pipeline = self._client.pipeline(transaction=True)
        pipeline.xadd(
            self._dead_stream,
            {
                "job_id": job_id,
                "attempts": str(attempts),
                "error": error[:500],
            },
        )
        pipeline.xack(self._stream, self._group, message_id)
        pipeline.execute()

    def status(self) -> dict:
        """Estado real de la cola para el readiness (sin valores inventados)."""
        self._ensure_group()
        pending_info = self._client.xpending(self._stream, self._group)
        if isinstance(pending_info, dict):
            pending = int(pending_info.get("pending", 0))
        else:
            pending = int(pending_info[0])
        length = int(self._client.xlen(self._stream))
        return {
            "backend": "redis",
            "active": pending,
            "idle": max(0, self._max_workers - pending),
            "max_workers": self._max_workers,
            "shutdown": False,
            "claimed": pending,
            "pending": pending,
            "stream_length": length,
        }
