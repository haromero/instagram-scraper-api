"""Contrato común de los backends de datos.

Cada backend devuelve diccionarios con la misma forma que los esquemas
Pydantic de ``app/schemas.py``. La fachada ``app/services/scraper.py`` elige
la implementación según ``IG_SCRAPER_BACKEND``; routers y jobs no saben (ni
necesitan saber) de dónde vienen los datos.
"""

from typing import Protocol


class ScraperBackend(Protocol):
    """Interfaz mínima que debe cumplir un backend de scraping."""

    def get_profile(self, username: str) -> dict: ...

    def get_posts(self, username: str, count: int = 120) -> list[dict]: ...

    def get_post(self, shortcode: str) -> dict: ...

    def get_comments(self, shortcode: str, count: int = 120) -> list[dict]: ...

    def get_likes(self, shortcode: str, count: int = 120) -> list[dict]: ...

    def get_followers(self, username: str, count: int = 120) -> list[dict]: ...

    def get_following(self, username: str, count: int = 120) -> list[dict]: ...

    def get_users(self, seed: str, count: int = 120) -> list[dict]:
        """Compatibilidad con la firma antigua ``"followers:<usuario>"``."""
        ...

    def get_stories(self, username: str, count: int = 10) -> list[dict]: ...

    def get_highlights(self, username: str, count: int = 10) -> list[dict]: ...

    def get_hashtag(self, hashtag: str) -> dict: ...

    def get_hashtag_posts(
        self, hashtag: str, mode: str = "recent", count: int = 120
    ) -> list[dict]: ...

    def search(
        self, query: str, kind: str = "all", count: int = 120
    ) -> list[dict]: ...

    def build_result(
        self, job_type: str, target: dict, options: dict
    ) -> list[dict]: ...
