"""Ciclo de vida de los jobs: creación, ejecución simulada y serialización.

La ejecución corre en un ``ThreadPoolExecutor`` acotado: el número de hilos
nunca supera ``IG_CONCURRENT_JOBS_LIMIT``, y ``enqueue`` aplica además una
cuota por cliente y una cuota mensual de peticiones.
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .. import locks
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..errors import APIError
from ..models import Job, ProfileCache
from ..queue import RedisJobQueue
from ..redis_backend import get_client
from . import scraper, webhooks

logger = logging.getLogger(__name__)

_settings = get_settings()
_executor = ThreadPoolExecutor(
    max_workers=_settings.concurrent_jobs_limit,
    thread_name_prefix="ig-job",
)
_active_lock = threading.Lock()
_active_jobs = 0


class ThreadQueue:
    """Cola en proceso: mantiene el arranque local sin Redis."""

    backend = "thread"

    def publish(self, job_id: str) -> None:
        global _active_jobs
        with _active_lock:
            _active_jobs += 1
        try:
            _executor.submit(_run_and_release, job_id)
        except Exception:
            with _active_lock:
                _active_jobs -= 1
            raise

    def status(self) -> dict:
        with _active_lock:
            active = _active_jobs
        max_workers = get_settings().concurrent_jobs_limit
        return {
            "backend": self.backend,
            "active": active,
            "idle": max(0, max_workers - active),
            "max_workers": max_workers,
            "shutdown": bool(getattr(_executor, "_shutdown", False)),
        }

    def close(self) -> None:
        _executor.shutdown(wait=False, cancel_futures=True)


# Se sustituye en el arranque cuando IG_JOB_BACKEND=redis.
_queue: ThreadQueue | RedisJobQueue = ThreadQueue()


def configure(queue: ThreadQueue | RedisJobQueue) -> None:
    global _queue
    _queue = queue


def build_job_queue(settings: Settings | None = None, client=None):
    """Construye la cola indicada por ``IG_JOB_BACKEND``."""
    settings = settings or get_settings()
    if settings.job_backend == "redis":
        return RedisJobQueue(client if client is not None else get_client(), settings)
    return ThreadQueue()


def pool_status() -> dict:
    """Estado real de la cola para el readiness (sin valores inventados)."""
    try:
        return _queue.status()
    except Exception:
        logger.exception("No se pudo consultar el estado de la cola de jobs")
        return {
            "backend": _queue.backend,
            "active": 0,
            "idle": 0,
            "max_workers": get_settings().concurrent_jobs_limit,
            "shutdown": True,
        }


def _run_and_release(job_id: str) -> None:
    global _active_jobs
    try:
        run_job(job_id)
    finally:
        with _active_lock:
            _active_jobs -= 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def schedule(job_id: str) -> None:
    """Publica el job en la cola configurada (hilos o Redis Streams)."""
    _queue.publish(job_id)


def shutdown() -> None:
    """Cierra la cola local al apagar la aplicación."""
    _queue.close()


def _assert_quota(db, owner: str) -> None:
    settings = get_settings()
    now = _now()

    active = (
        db.query(Job)
        .filter(Job.owner == owner, Job.status.in_(["queued", "running"]))
        .count()
    )
    if active >= settings.concurrent_jobs_limit:
        raise APIError(
            429,
            "CONCURRENCY_LIMIT",
            "Has alcanzado el límite de jobs concurrentes.",
            {
                "concurrent_jobs": active,
                "concurrent_jobs_limit": settings.concurrent_jobs_limit,
            },
            headers={"Retry-After": "5"},
        )

    used_this_month = (
        db.query(Job)
        .filter(Job.owner == owner, Job.created_at >= _month_start(now))
        .count()
    ) * 10
    if used_this_month >= settings.requests_limit:
        raise APIError(
            429,
            "QUOTA_EXCEEDED",
            "Has agotado la cuota mensual de peticiones.",
            {
                "requests_used": used_this_month,
                "requests_limit": settings.requests_limit,
            },
        )


def create_job(
    db,
    owner: str,
    job_type: str,
    target: dict,
    options: dict | None = None,
) -> Job:
    settings = get_settings()
    now = _now()
    job = Job(
        id="job_" + uuid.uuid4().hex[:12],
        owner=owner,
        type=job_type,
        target=json.dumps(target),
        options=json.dumps(options or {}),
        status="queued",
        attempts=0,
        items_scraped=0,
        items_total=0,
        created_at=now,
        estimated_completion=now + timedelta(seconds=settings.job_simulation_seconds),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue(
    db,
    owner: str,
    job_type: str,
    target: dict,
    options: dict | None = None,
) -> Job:
    """Valida cuotas, crea el job y lo lanza en segundo plano.

    El candado puede ser local (default) o distribuido (Redis). Se mantiene
    durante la comprobación y la inserción para que dos workers no lean el
    mismo contador y superen la cuota entre los dos.
    """
    with locks.quota_lock.hold(f"job-quota:{owner}"):
        _assert_quota(db, owner)
        job = create_job(
            db, owner=owner, job_type=job_type, target=target, options=options
        )
    schedule(job.id)
    return job


def serialize_job(job: Job) -> dict:
    total = job.items_total or 0
    scraped = job.items_scraped or 0
    return {
        "job_id": job.id,
        "type": job.type,
        "status": job.status,
        "progress": {
            "items_scraped": scraped,
            "items_total": total,
            "percent": int(round(100 * scraped / total)) if total else 0,
        },
        "result_url": f"/api/v1/jobs/{job.id}/result",
        "poll_url": f"/api/v1/jobs/{job.id}",
        "error": json.loads(job.error) if job.error else None,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "estimated_completion": job.estimated_completion,
    }


def _set_failed(db, job_id: str, code: str = "JOB_FAILED") -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = "failed"
    job.error = json.dumps(
        {
            "code": code,
            "message": "El job falló durante la ejecución.",
            "request_id": None,
            "details": {},
        }
    )
    job.finished_at = _now()
    db.commit()
    # El evento se entrega con su propia sesión: un webhook caído no afecta
    # al estado del job ni al worker que lo ejecuta.
    webhooks.notify("job.failed", job_id)


def mark_job_failed(job_id: str, code: str = "JOB_FAILED") -> None:
    """Marca un job como fallido (lo usa el worker al agotar reintentos)."""
    db = SessionLocal()
    try:
        _set_failed(db, job_id, code)
    finally:
        db.close()


def reclaim_stale(job_id: str, visibility_seconds: int) -> bool:
    """Devuelve a la cola un job cuyo worker murió; cuenta el intento."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != "running" or job.started_at is None:
            return False
        threshold = _now() - timedelta(seconds=visibility_seconds)
        started_at = job.started_at
        if started_at.tzinfo is None:
            threshold = threshold.replace(tzinfo=None)
        if started_at >= threshold:
            return False
        job.status = "queued"
        job.started_at = None
        job.attempts = (job.attempts or 0) + 1
        db.commit()
        return True
    finally:
        db.close()


def job_attempts(job_id: str) -> int:
    """Intentos persistidos del job (para aplicar el máximo del worker)."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        return int(job.attempts or 0) if job is not None else 0
    finally:
        db.close()


def job_status(job_id: str) -> str | None:
    """Estado actual del job, para saber si el mensaje puede confirmarse."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        return job.status if job is not None else None
    finally:
        db.close()


def queued_job_ids(older_than_seconds: int) -> list[str]:
    """Jobs en cola lo bastante antiguos como para recuperarlos al arrancar.

    La Key Value gratuita de Render no persiste: si Redis se reinicia, los
    mensajes desaparecen pero los jobs siguen en PostgreSQL. El worker los
    republica una vez al iniciar. La ejecución es idempotente (``run_job`` solo
    actúa si el job sigue en ``queued``).
    """
    db = SessionLocal()
    try:
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        if db.get_bind().dialect.name == "sqlite":
            cutoff = cutoff.replace(tzinfo=None)
        return [
            job.id
            for job in db.query(Job)
            .filter(Job.status == "queued", Job.created_at <= cutoff)
            .all()
        ]
    finally:
        db.close()


def run_job(job_id: str, *, fail_fast: bool = False) -> None:
    """Simula el scraping avanzando el progreso y guardando el resultado.

    Con ``fail_fast`` el error se propaga al worker para que decida entre
    reintentar o enviar el job al dead letter. Sin él se mantiene el
    comportamiento histórico del pool en proceso.
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != "queued":
            return

        job.status = "running"
        job.started_at = _now()
        db.commit()

        target = json.loads(job.target or "{}")
        options = json.loads(job.options or "{}")
        items = scraper.build_result(job.type, target, options)
        total = max(1, len(items))
        job.items_total = total
        db.commit()

        steps = max(1, settings.job_steps)
        delay = max(0.0, settings.job_simulation_seconds / steps)
        for step in range(1, steps + 1):
            time.sleep(delay)
            db.refresh(job)
            if job.status == "cancelled":
                webhooks.notify("job.cancelled", job_id)
                return
            job.items_scraped = int(total * step / steps)
            db.commit()

        db.refresh(job)
        if job.status == "cancelled":
            webhooks.notify("job.cancelled", job_id)
            return

        job.status = "completed"
        job.items_scraped = total
        job.items_total = total
        job.result = json.dumps(items, default=str)
        job.finished_at = _now()
        db.commit()
        webhooks.notify("job.completed", job_id)

        # Refresca la caché del perfil si el job era de tipo "profile".
        if job.type == "profile" and items:
            profile = items[0]
            cached = db.get(ProfileCache, profile["username"])
            if cached is None:
                cached = ProfileCache(
                    username=profile["username"], data="", scraped_at=_now()
                )
                db.add(cached)
            cached.data = json.dumps(profile, default=str)
            cached.scraped_at = _now()
            db.commit()

    except Exception:  # pragma: no cover - salvaguarda
        # El detalle (rutas, SQL, configuración) se queda en el log del servidor.
        logger.exception("El job %s falló durante la ejecución", job_id)
        db.rollback()
        _set_failed(db, job_id)
        if fail_fast:
            raise
    finally:
        db.close()
