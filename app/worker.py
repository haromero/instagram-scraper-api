"""Proceso worker: consume la cola Redis fuera del servidor HTTP.

Uso:
    python -m app.worker

El job se ejecuta con ``fail_fast=True``: si falla, el worker decide entre
republicarlo con un intento más o enviarlo al stream de dead letters. Un job
cuyo worker murió se recupera por visibilidad y vuelve a encolarse.
"""

from __future__ import annotations

import logging
import signal
import threading

from .config import Settings, get_settings
from .database import engine
from .db_migrations import upgrade_database
from .queue import RedisJobQueue
from .redis_backend import build_redis_client, set_client
from .services import jobs as jobs_service
from .services import scraper

logger = logging.getLogger(__name__)
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class Worker:
    """Bucle de consumo de un consumer group de Redis Streams."""

    def __init__(self, queue: RedisJobQueue, settings: Settings | None = None) -> None:
        self._queue = queue
        self._settings = settings or get_settings()

    def process_once(self) -> int:
        """Procesa un lote y devuelve cuántos mensajes se han reclamado."""
        claimed = self._queue.claim()
        for item in claimed:
            jobs_service.reclaim_stale(
                item.job_id, self._settings.job_visibility_seconds
            )
            attempts = max(item.attempts, jobs_service.job_attempts(item.job_id))

            if attempts >= self._settings.job_max_attempts:
                jobs_service.mark_job_failed(item.job_id)
                self._queue.dead_letter(
                    item.message_id,
                    item.job_id,
                    attempts,
                    "Se agotaron los intentos de ejecución.",
                )
                continue

            try:
                jobs_service.run_job(item.job_id, fail_fast=True)
            except Exception as exc:
                logger.warning("Job %s falló (intento %s): %s", item.job_id, attempts, exc)
                if attempts + 1 >= self._settings.job_max_attempts:
                    jobs_service.mark_job_failed(item.job_id)
                    self._queue.dead_letter(
                        item.message_id, item.job_id, attempts + 1, str(exc)
                    )
                else:
                    self._queue.retry(item.message_id, item.job_id, attempts)
                continue

            status = jobs_service.job_status(item.job_id)
            if status in _TERMINAL_STATUSES:
                self._queue.ack(item.message_id)
            else:
                # Cancelado a medias, job desaparecido o ejecución incompleta:
                # se devuelve a la cola en lugar de perder el mensaje.
                self._queue.retry(item.message_id, item.job_id, attempts)
        return len(claimed)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                processed = self.process_once()
            except Exception:
                logger.exception("Error al consultar la cola de jobs")
                processed = 0
            if processed == 0:
                stop.wait(self._settings.job_poll_seconds)


def main() -> None:
    settings = get_settings()
    if settings.job_backend != "redis":
        raise SystemExit("El worker necesita IG_JOB_BACKEND=redis")

    client = build_redis_client(settings)
    if client is None:
        raise SystemExit("El worker necesita IG_RATE_LIMIT_BACKEND=redis")
    set_client(client)
    upgrade_database(engine)

    queue = RedisJobQueue(client, settings)
    # Recupera jobs que quedaron en la BD si Redis perdió sus mensajes.
    for job_id in jobs_service.queued_job_ids(settings.job_visibility_seconds):
        queue.publish(job_id)
        logger.info("Republicado job %s al arrancar", job_id)

    worker = Worker(queue, settings)
    stop = threading.Event()

    def _handle_signal(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Worker iniciado en %s", settings.job_queue_stream)
    try:
        worker.run(stop)
    finally:
        scraper.reset_backend()
        client.close()
        logger.info("Worker detenido")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
