"""Fachada del scraper: elige el backend y conserva las firmas públicas.

Los routers y ``jobs.py`` importan este módulo y llaman a funciones normales.
El backend real se selecciona con ``IG_SCRAPER_BACKEND``:

- ``simulated`` (por defecto): datos deterministas, sin red.
- ``bluesky``: API pública de Bluesky/AT Protocol.

La instancia se crea de forma perezosa para que importar la app no abra
clientes HTTP ni dependa de la configuración del backend.
"""

import threading

from ..config import Settings, get_settings
from .scraper_base import ScraperBackend

_backend: ScraperBackend | None = None
_lock = threading.Lock()


def _build_backend(settings: Settings | None = None) -> ScraperBackend:
    settings = settings or get_settings()
    if settings.scraper_backend == "bluesky":
        from .scraper_bluesky import BlueskyScraper

        return BlueskyScraper.from_settings(settings)

    from .scraper_simulated import SimulatedScraper

    return SimulatedScraper()


def _get_backend() -> ScraperBackend:
    global _backend
    if _backend is None:
        with _lock:
            if _backend is None:
                _backend = _build_backend()
    return _backend


def reset_backend() -> None:
    """Cierra el backend actual (si tiene cliente HTTP) y lo olvida."""
    global _backend
    with _lock:
        backend, _backend = _backend, None
    if backend is not None:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def get_profile(username: str) -> dict:
    return _get_backend().get_profile(username)


def get_posts(username: str, count: int = 120) -> list[dict]:
    return _get_backend().get_posts(username, count=count)


def get_post(shortcode: str) -> dict:
    return _get_backend().get_post(shortcode)


def get_comments(shortcode: str, count: int = 120) -> list[dict]:
    return _get_backend().get_comments(shortcode, count=count)


def get_likes(shortcode: str, count: int = 120) -> list[dict]:
    return _get_backend().get_likes(shortcode, count=count)


def get_followers(username: str, count: int = 120) -> list[dict]:
    return _get_backend().get_followers(username, count=count)


def get_following(username: str, count: int = 120) -> list[dict]:
    return _get_backend().get_following(username, count=count)


def get_users(seed: str, count: int = 120) -> list[dict]:
    return _get_backend().get_users(seed, count=count)


def get_stories(username: str, count: int = 10) -> list[dict]:
    return _get_backend().get_stories(username, count=count)


def get_highlights(username: str, count: int = 10) -> list[dict]:
    return _get_backend().get_highlights(username, count=count)


def get_hashtag(hashtag: str) -> dict:
    return _get_backend().get_hashtag(hashtag)


def get_hashtag_posts(
    hashtag: str, mode: str = "recent", count: int = 120
) -> list[dict]:
    return _get_backend().get_hashtag_posts(hashtag, mode=mode, count=count)


def search(query: str, kind: str = "all", count: int = 120) -> list[dict]:
    return _get_backend().search(query, kind=kind, count=count)


def build_result(job_type: str, target: dict, options: dict) -> list[dict]:
    return _get_backend().build_result(job_type, target, options)
