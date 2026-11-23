"""Adaptador de datos reales sobre la API pública de Bluesky (AT Protocol).

No necesita credenciales para perfiles, feeds, seguidores, follows, hilos ni
likes. La búsqueda de posts (``app.bsky.feed.searchPosts``) sí puede exigir
una cuenta gratuita: si configuras ``IG_BLUESKY_HANDLE`` e
``IG_BLUESKY_APP_PASSWORD`` el adaptador crea una sesión en el PDS y la usa
para esa llamada.

El módulo solo habla HTTP: no depende de SDKs, de cuentas de Instagram ni de
proxies. Los errores de red y de cuota se traducen a ``APIError`` para que la
API mantenga su envelope uniforme.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx

from ..config import Settings
from ..errors import APIError

_ISO_Z = re.compile(r"Z$")
_TAG_IN_TEXT = re.compile(r"#(\w{1,64})")
_MAX_PAGE = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime:
    if not value:
        return _now()
    text = _ISO_Z.sub("+00:00", str(value))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _actor(value: str) -> str:
    """Normaliza un handle (quita la @) sin tocar DIDs ni AT URIs."""
    actor = (value or "").strip()
    if actor.startswith("@"):
        actor = actor[1:]
    return actor


def _embed_kind(embed: dict | None) -> str:
    if not embed:
        return "none"
    etype = str(embed.get("$type") or "")
    if "video" in etype or embed.get("playlist"):
        return "video"
    if "images" in etype or "images" in embed:
        return "images"
    if "external" in etype or "external" in embed:
        return "external"
    if "recordWithMedia" in etype:
        return _embed_kind(embed.get("media") or {})
    return "none"


def _extract_media(post: dict) -> tuple[list[str], str | None, str]:
    """Devuelve (urls, thumbnail, tipo) a partir del embed del post."""
    embed = post.get("embed") or {}
    kind = _embed_kind(embed)

    if kind == "video":
        video = embed if "playlist" in embed or "thumbnail" in embed else embed.get("media") or {}
        thumbnail = video.get("thumbnail")
        url = video.get("playlist") or thumbnail
        return ([url] if url else []), thumbnail, "video"

    if kind == "images":
        images = embed.get("images")
        if images is None:
            images = (embed.get("media") or {}).get("images") or []
        urls = [
            image.get("fullsize") or image.get("thumb")
            for image in images
            if image.get("fullsize") or image.get("thumb")
        ]
        thumb = images[0].get("thumb") if images else None
        return urls, thumb or (urls[0] if urls else None), "carousel" if len(urls) > 1 else "image"

    if kind == "external":
        external = embed.get("external") or (embed.get("media") or {}).get("external") or {}
        thumb = external.get("thumb")
        return ([thumb] if thumb else []), thumb, "image"

    return [], None, "text"


def _map_user(profile: dict) -> dict:
    did = profile.get("did")
    handle = profile.get("handle")
    verification = profile.get("verification") or {}
    return {
        "username": handle or did or "unknown",
        "user_id": did,
        "full_name": profile.get("displayName"),
        # Un objeto verification no vacío con estado "invalid"/"expired" no
        # significa que la cuenta esté verificada.
        "is_verified": verification.get("verifiedStatus") == "valid",
        "is_private": False,
        "profile_pic_url": profile.get("avatar"),
    }


def _extract_tags(record: dict) -> set[str]:
    tags: set[str] = set()
    for facet in record.get("facets") or []:
        for feature in facet.get("features") or []:
            tag = feature.get("tag")
            if tag:
                tags.add(str(tag).lstrip("#").lower())
    for match in _TAG_IN_TEXT.findall(record.get("text") or ""):
        tags.add(match.lower())
    return tags


class BlueskyScraper:
    """Implementación de ``ScraperBackend`` sobre el AppView público."""

    def __init__(
        self,
        *,
        api_base: str = "https://public.api.bsky.app",
        appview_base: str = "https://api.bsky.app",
        pds_url: str = "https://bsky.social",
        handle: str | None = None,
        app_password: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._appview_base = appview_base.rstrip("/")
        self._pds_url = pds_url.rstrip("/")
        self._handle = _actor(handle) if handle else None
        self._app_password = app_password
        self._max_retries = max(1, int(max_retries))
        self._backoff = max(0.0, float(backoff_seconds))
        self._session_lock = threading.Lock()
        self._access_jwt: str | None = None
        self._client = client or httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "instagram-scraper-api/1.2 (Bluesky backend)"},
        )

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_settings(cls, settings: Settings) -> "BlueskyScraper":
        password = (
            settings.bluesky_app_password.get_secret_value()
            if settings.bluesky_app_password is not None
            else None
        )
        return cls(
            api_base=settings.bluesky_api_base,
            appview_base=settings.bluesky_appview_base,
            pds_url=settings.bluesky_pds_url,
            handle=settings.bluesky_handle,
            app_password=password,
            timeout=settings.bluesky_timeout_seconds,
            max_retries=settings.bluesky_max_retries,
            backoff_seconds=settings.bluesky_backoff_seconds,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ HTTP
    def _ensure_session(self) -> str:
        """Crea (y cachea) una sesión en el PDS con la app password."""
        if self._access_jwt:
            return self._access_jwt
        with self._session_lock:
            if self._access_jwt:
                return self._access_jwt
            if not self._handle or not self._app_password:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "Esta operación de Bluesky requiere una cuenta configurada "
                    "(IG_BLUESKY_HANDLE e IG_BLUESKY_APP_PASSWORD).",
                    {"upstream": "bluesky", "reason": "credentials_required"},
                )
            url = f"{self._pds_url}/xrpc/com.atproto.server.createSession"
            try:
                response = self._client.post(
                    url,
                    json={
                        "identifier": self._handle,
                        "password": self._app_password,
                    },
                )
            except httpx.HTTPError as exc:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "No se pudo conectar con el PDS de Bluesky.",
                    {"upstream": "bluesky"},
                ) from exc

            if response.status_code in (400, 401):
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "Las credenciales de Bluesky no son válidas.",
                    {"upstream": "bluesky", "reason": "invalid_credentials"},
                )
            if response.status_code >= 400:
                raise self._upstream_error(response)

            try:
                token = response.json().get("accessJwt")
            except ValueError as exc:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "El PDS de Bluesky devolvió una respuesta no válida.",
                    {"upstream": "bluesky"},
                ) from exc
            if not token:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "El PDS de Bluesky no devolvió un token de acceso.",
                    {"upstream": "bluesky"},
                )
            self._access_jwt = token
            return token

    def _request(
        self,
        endpoint: str,
        params: Iterable[tuple[str, Any]] | dict[str, Any] | None = None,
        *,
        base: str | None = None,
        authenticated: bool = False,
    ) -> dict:
        """GET a ``/xrpc/<endpoint>`` con reintentos y mapeo de errores."""
        base_url = (base or self._api_base).rstrip("/")
        url = f"{base_url}/xrpc/{endpoint}"
        refreshed = False
        # Un 401 por accessJwt caducado merece un intento extra para renovar la
        # sesión, incluso cuando max_retries=1.
        max_attempts = self._max_retries + (1 if authenticated else 0)

        for attempt in range(1, max_attempts + 1):
            headers: dict[str, str] = {}
            if authenticated:
                headers["Authorization"] = f"Bearer {self._ensure_session()}"

            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise APIError(
                        503,
                        "UPSTREAM_UNAVAILABLE",
                        "Bluesky no respondió a tiempo.",
                        {"upstream": "bluesky"},
                    ) from exc
                self._sleep(attempt)
                continue
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise APIError(
                        503,
                        "UPSTREAM_UNAVAILABLE",
                        "No se pudo conectar con Bluesky.",
                        {"upstream": "bluesky"},
                    ) from exc
                self._sleep(attempt)
                continue

            if response.status_code == 429:
                if attempt >= self._max_retries:
                    raise APIError(
                        429,
                        "RATE_LIMITED",
                        "Bluesky limitó las peticiones; inténtalo más tarde.",
                        {"upstream": "bluesky"},
                        headers={
                            "Retry-After": response.headers.get("Retry-After", "5")
                        },
                    )
                self._sleep(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise self._upstream_error(response)
                self._sleep(attempt)
                continue

            if response.status_code == 401 and authenticated and not refreshed:
                # El accessJwt caducó: se renueva una vez y se reintenta.
                self._access_jwt = None
                refreshed = True
                continue

            if response.status_code == 400:
                message = self._message(response)
                if "not found" in message.lower() or "could not find" in message.lower():
                    raise APIError(404, "NOT_FOUND", "El recurso no existe en Bluesky.")
                raise APIError(
                    400,
                    "INVALID_REQUEST",
                    "Bluesky rechazó la petición.",
                    {"upstream": "bluesky", "reason": message or None},
                )

            if response.status_code == 403:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "Bluesky rechazó la petición (403). Si persiste, configura "
                    "una cuenta de Bluesky para las operaciones autenticadas.",
                    {"upstream": "bluesky", "status": 403},
                )

            if response.status_code == 404:
                raise APIError(404, "NOT_FOUND", "El recurso no existe en Bluesky.")

            if response.status_code >= 400:
                raise self._upstream_error(response)

            try:
                return response.json()
            except ValueError as exc:
                raise APIError(
                    503,
                    "UPSTREAM_UNAVAILABLE",
                    "Bluesky devolvió una respuesta no válida.",
                    {"upstream": "bluesky"},
                ) from exc

        # Inalcanzable: el bucle siempre retorna o lanza.
        raise APIError(
            503,
            "UPSTREAM_UNAVAILABLE",
            "No se pudo completar la petición a Bluesky.",
            {"upstream": "bluesky"},
        )

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self._backoff * (2 ** (attempt - 1))
        else:
            delay = self._backoff * (2 ** (attempt - 1))
        if delay > 0:
            time.sleep(min(delay, 30.0))

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            return str(response.json().get("message") or "")
        except ValueError:
            return ""

    def _upstream_error(self, response: httpx.Response) -> APIError:
        details = {"upstream": "bluesky", "status": response.status_code}
        message = self._message(response)
        if message:
            details["reason"] = message
        return APIError(
            503,
            "UPSTREAM_UNAVAILABLE",
            "El servicio de Bluesky no está disponible.",
            details,
        )

    def _collect(
        self,
        endpoint: str,
        params: dict[str, Any] | Iterable[tuple[str, Any]],
        key: str,
        count: int,
        *,
        base: str | None = None,
        authenticated: bool = False,
        transform: Callable[[dict], dict | None] | None = None,
    ) -> list[dict]:
        """Recorre cursores hasta reunir ``count`` elementos."""
        count = max(1, int(count))
        items: list[dict] = []
        cursor: str | None = None
        while len(items) < count:
            page: list[tuple[str, Any]] = list(
                params.items() if isinstance(params, dict) else params
            )
            page.append(("limit", min(_MAX_PAGE, count - len(items))))
            if cursor:
                page.append(("cursor", cursor))
            data = self._request(
                endpoint, page, base=base, authenticated=authenticated
            )
            batch = data.get(key) or []
            if transform is not None:
                mapped = [transform(item) for item in batch]
                batch = [item for item in mapped if item is not None]
            items.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not data.get(key):
                break
        return items[:count]

    # ------------------------------------------------------------------ mapeo
    def _map_post(self, post: dict) -> dict | None:
        uri = post.get("uri")
        if not uri:
            return None
        author = post.get("author") or {}
        record = post.get("record") or {}
        media_urls, thumbnail, media_type = _extract_media(post)
        rkey = str(uri).rsplit("/", 1)[-1]
        did = author.get("did")
        return {
            "id": str(uri),
            "shortcode": f"{did}|{rkey}" if did else rkey,
            "type": media_type,
            "caption": record.get("text") or None,
            "like_count": int(post.get("likeCount") or 0),
            "comment_count": int(post.get("replyCount") or 0),
            "taken_at": _parse_dt(record.get("createdAt")),
            "media_urls": media_urls,
            "thumbnail_url": thumbnail,
            "owner": _map_user(author) if author else None,
            "scraped_at": _now(),
        }

    def _map_comment(self, post: dict) -> dict | None:
        uri = post.get("uri")
        if not uri:
            return None
        record = post.get("record") or {}
        author = post.get("author") or {}
        return {
            "id": str(uri),
            "text": record.get("text") or "",
            "author": _map_user(author),
            "like_count": int(post.get("likeCount") or 0),
            "replies_count": int(post.get("replyCount") or 0),
            "created_at": _parse_dt(record.get("createdAt")),
        }

    @staticmethod
    def _post_uri(shortcode: str) -> str:
        """Acepta un AT URI completo o el compuesto ``did|rkey``."""
        value = (shortcode or "").strip()
        if value.startswith("at://"):
            return value
        if "|" in value:
            did, _, rkey = value.partition("|")
            if did and rkey:
                return f"at://{did}/app.bsky.feed.post/{rkey}"
        raise APIError(
            422,
            "VALIDATION_ERROR",
            "El shortcode debe ser un AT URI o el formato 'did|rkey'.",
            {"field": "shortcode"},
        )

    # ------------------------------------------------------------------ API
    def get_profile(self, username: str) -> dict:
        data = self._request(
            "app.bsky.actor.getProfile", {"actor": _actor(username)}
        )
        return {
            "username": data.get("handle") or _actor(username),
            "user_id": data.get("did"),
            "full_name": data.get("displayName"),
            "biography": data.get("description"),
            "is_private": False,
            "is_verified": bool(data.get("verification")),
            "followers_count": int(data.get("followersCount") or 0),
            "following_count": int(data.get("followsCount") or 0),
            "posts_count": int(data.get("postsCount") or 0),
            "profile_pic_url": data.get("avatar"),
            "external_url": data.get("website"),
            "scraped_at": _now(),
        }

    def get_posts(self, username: str, count: int = 120) -> list[dict]:
        return self._collect(
            "app.bsky.feed.getAuthorFeed",
            {"actor": _actor(username), "filter": "posts_with_replies"},
            "feed",
            count,
            transform=lambda item: self._map_post(item.get("post") or {}),
        )

    def get_post(self, shortcode: str) -> dict:
        uri = self._post_uri(shortcode)
        data = self._request(
            "app.bsky.feed.getPosts", [("uris", uri)]
        )
        posts = data.get("posts") or []
        if not posts:
            raise APIError(404, "NOT_FOUND", "El post no existe en Bluesky.")
        mapped = self._map_post(posts[0])
        if mapped is None:
            raise APIError(404, "NOT_FOUND", "El post no existe en Bluesky.")
        return mapped

    def get_comments(self, shortcode: str, count: int = 120) -> list[dict]:
        uri = self._post_uri(shortcode)
        data = self._request(
            "app.bsky.feed.getPostThread",
            {"uri": uri, "depth": 10, "parentHeight": 0},
        )
        thread = data.get("thread") or {}
        if not thread.get("post"):
            raise APIError(404, "NOT_FOUND", "El post no existe en Bluesky.")

        comments: list[dict] = []
        queue: list[dict] = list(thread.get("replies") or [])
        while queue and len(comments) < count:
            node = queue.pop(0)
            post = node.get("post")
            if post:
                mapped = self._map_comment(post)
                if mapped is not None:
                    comments.append(mapped)
            queue.extend(node.get("replies") or [])
        return comments[:count]

    def get_likes(self, shortcode: str, count: int = 120) -> list[dict]:
        uri = self._post_uri(shortcode)
        return self._collect(
            "app.bsky.feed.getLikes",
            {"uri": uri},
            "likes",
            count,
            transform=lambda item: _map_user(item.get("actor") or {}),
        )

    def get_followers(self, username: str, count: int = 120) -> list[dict]:
        return self._collect(
            "app.bsky.graph.getFollowers",
            {"actor": _actor(username)},
            "followers",
            count,
            transform=_map_user,
        )

    def get_following(self, username: str, count: int = 120) -> list[dict]:
        return self._collect(
            "app.bsky.graph.getFollows",
            {"actor": _actor(username)},
            "follows",
            count,
            transform=_map_user,
        )

    def get_users(self, seed: str, count: int = 120) -> list[dict]:
        prefix, _, actor = (seed or "").partition(":")
        if prefix == "followers":
            return self.get_followers(actor, count=count)
        if prefix == "following":
            return self.get_following(actor, count=count)
        if prefix == "likes":
            return self.get_likes(actor, count=count)
        raise APIError(
            400,
            "INVALID_REQUEST",
            "El backend de Bluesky no admite búsquedas genéricas de usuarios.",
        )

    def get_stories(self, username: str, count: int = 10) -> list[dict]:
        """Bluesky no tiene stories: se devuelve una lista vacía."""
        return []

    def get_highlights(self, username: str, count: int = 10) -> list[dict]:
        """Bluesky no tiene highlights: se devuelve una lista vacía."""
        return []

    def _search_base(self) -> tuple[str, bool]:
        """``searchPosts`` usa el PDS autenticado si hay credenciales."""
        if self._handle and self._app_password:
            return self._pds_url, True
        return self._appview_base, False

    def _search_posts(
        self, query: str, *, tag: str | None = None, mode: str = "recent", count: int = 120
    ) -> list[dict]:
        base, authenticated = self._search_base()
        params: list[tuple[str, Any]] = [("q", query)]
        if tag:
            params.append(("tag", tag.lstrip("#")))
        params.append(("sort", "top" if mode == "top" else "latest"))

        if not authenticated:
            # El AppView público permite la primera página de searchPosts pero
            # bloquea el cursor con 403. Se devuelve una sola página (máx. 100).
            page = list(params)
            page.append(("limit", min(_MAX_PAGE, max(1, int(count)))))
            data = self._request(
                "app.bsky.feed.searchPosts", page, base=base
            )
            posts = data.get("posts") or []
            mapped = [self._map_post(post) for post in posts]
            return [post for post in mapped if post is not None][:count]

        return self._collect(
            "app.bsky.feed.searchPosts",
            params,
            "posts",
            count,
            base=base,
            authenticated=True,
            transform=self._map_post,
        )

    def get_hashtag_posts(
        self, hashtag: str, mode: str = "recent", count: int = 120
    ) -> list[dict]:
        tag = (hashtag or "").lstrip("#")
        return self._search_posts(f"#{tag}", tag=tag, mode=mode, count=count)

    def get_hashtag(self, hashtag: str) -> dict:
        tag = (hashtag or "").lstrip("#")
        posts = self.get_hashtag_posts(tag, mode="top", count=25)
        return {
            "name": tag,
            # Bluesky no expone un contador global de hashtags: se informa del
            # número de posts recuperados en esta consulta.
            "media_count": len(posts),
            "top_posts": [
                {
                    "shortcode": post["shortcode"],
                    "like_count": post["like_count"] or 0,
                    "thumbnail_url": post["thumbnail_url"],
                }
                for post in posts[:5]
            ],
            "scraped_at": _now(),
        }

    def search(
        self, query: str, kind: str = "all", count: int = 120
    ) -> list[dict]:
        results: list[dict] = []
        if kind in ("user", "all"):
            actors = self._collect(
                "app.bsky.actor.searchActors",
                {"q": query},
                "actors",
                count,
                transform=lambda actor: {"kind": "user", **_map_user(actor)},
            )
            results.extend(actors)

        if kind in ("hashtag", "all"):
            try:
                posts = self._search_posts(
                    query, tag=query, mode="top", count=min(count, 100)
                )
            except APIError:
                if kind == "hashtag":
                    raise
                posts = []
            counts: dict[str, int] = {}
            for post in posts:
                # ``_map_post`` no conserva los facets; se usan los tags del
                # texto y el propio término buscado.
                for tag in _TAG_IN_TEXT.findall(post.get("caption") or ""):
                    counts[tag.lower()] = counts.get(tag.lower(), 0) + 1
            for tag, total in sorted(counts.items(), key=lambda item: -item[1]):
                results.append(
                    {"kind": "hashtag", "name": tag, "media_count": total}
                )

        return results

    def build_result(
        self, job_type: str, target: dict, options: dict
    ) -> list[dict]:
        limit = min(int(options.get("limit") or 120), 200)
        username = target.get("username") or "unknown"
        shortcode = target.get("shortcode") or "unknown"
        hashtag = target.get("hashtag") or "unknown"
        query = target.get("query") or "unknown"

        if job_type == "profile":
            return [self.get_profile(username)]
        if job_type == "profile_posts":
            return self.get_posts(username, count=limit)
        if job_type == "profile_followers":
            return self.get_followers(username, count=limit)
        if job_type == "profile_following":
            return self.get_following(username, count=limit)
        if job_type == "post_comments":
            return self.get_comments(shortcode, count=limit)
        if job_type == "hashtag_posts":
            return self.get_hashtag_posts(
                hashtag, mode=options.get("mode") or "recent", count=limit
            )
        if job_type == "search":
            return self.search(query, kind=options.get("mode") or "all", count=limit)
        return []
