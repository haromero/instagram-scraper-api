"""Tests del adaptador de Bluesky con ``httpx.MockTransport`` (sin red).

Comprueban dos cosas a la vez:
1. El mapeo de la API de AT Protocol a los esquemas públicos de la API.
2. El comportamiento ante errores, reintentos y autenticación opcional.
"""

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from app.config import Settings, get_settings
from app.errors import APIError
from app.schemas import (
    Comment,
    Hashtag,
    Post,
    Profile,
    SearchResult,
    UserSummary,
)
from app.services import scraper
from app.services.scraper_bluesky import BlueskyScraper
from app.services.scraper_simulated import SimulatedScraper

DID = "did:plc:alice"
HANDLE = "alice.bsky.social"
POST_URI = f"at://{DID}/app.bsky.feed.post/3abc"


def _profile(**overrides) -> dict:
    data = {
        "did": DID,
        "handle": HANDLE,
        "displayName": "Alice",
        "description": "Perfil de prueba",
        "avatar": "https://cdn.test/alice.jpg",
        "followersCount": 42,
        "followsCount": 7,
        "postsCount": 3,
        "verification": {"verifiedStatus": "valid"},
    }
    data.update(overrides)
    return data


def _post(
    uri: str = POST_URI,
    *,
    text: str = "Hola #python",
    created_at: str = "2026-01-02T03:04:05.000Z",
    images: int | None = None,
    video: bool = False,
    likes: int = 9,
    replies: int = 2,
    author: dict | None = None,
) -> dict:
    post = {
        "uri": uri,
        "cid": "cid-1",
        "author": author or _profile(),
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": created_at,
        },
        "likeCount": likes,
        "replyCount": replies,
        "indexedAt": created_at,
    }
    if images is not None:
        post["embed"] = {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {
                    "thumb": f"https://cdn.test/{index}-thumb.jpg",
                    "fullsize": f"https://cdn.test/{index}.jpg",
                    "alt": "",
                }
                for index in range(images)
            ],
        }
    if video:
        post["embed"] = {
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.test/playlist.m3u8",
            "thumbnail": "https://cdn.test/video.jpg",
        }
    return post


def _transport(routes: dict[str, object]):
    """Crea un MockTransport que enruta por path y registra peticiones."""

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": "NotFound", "message": "no"})
        if isinstance(route, httpx.Response):
            return route
        if callable(route):
            return route(request)
        if isinstance(route, list):
            # Secuencia de respuestas para probar reintentos.
            response = route.pop(0)
            return response(request) if callable(response) else response
        return httpx.Response(200, json=route)

    return httpx.MockTransport(handler)


def _scraper(routes: dict[str, object], **kwargs) -> BlueskyScraper:
    defaults = {
        "api_base": "https://public.test",
        "appview_base": "https://appview.test",
        "pds_url": "https://pds.test",
        "max_retries": 1,
        "backoff_seconds": 0.0,
    }
    defaults.update(kwargs)
    return BlueskyScraper(transport=_transport(routes), **defaults)


# ---------------------------------------------------------------------------
# Mapeo de datos
# ---------------------------------------------------------------------------
def test_get_profile_matches_schema():
    adapter = _scraper(
        {"/xrpc/app.bsky.actor.getProfile": _profile()}
    )

    data = adapter.get_profile("@" + HANDLE)
    profile = Profile.model_validate(data)

    assert profile.username == HANDLE
    assert profile.user_id == DID
    assert profile.followers_count == 42
    assert profile.following_count == 7
    assert profile.is_verified is True
    assert profile.external_url is None


def test_get_posts_maps_media_and_schema():
    feed = {
        "feed": [
            {"post": _post()},
            {"post": _post(uri=POST_URI + "b", images=3, text="carousel")},
            {"post": _post(uri=POST_URI + "c", video=True, text="video")},
        ]
    }
    adapter = _scraper({"/xrpc/app.bsky.feed.getAuthorFeed": feed})

    posts = adapter.get_posts(HANDLE, count=3)
    validated = [Post.model_validate(post) for post in posts]

    assert [post.type for post in validated] == ["text", "carousel", "video"]
    assert validated[1].media_urls == [
        "https://cdn.test/0.jpg",
        "https://cdn.test/1.jpg",
        "https://cdn.test/2.jpg",
    ]
    assert validated[2].thumbnail_url == "https://cdn.test/video.jpg"
    assert validated[0].owner is not None
    assert validated[0].owner.username == HANDLE
    assert validated[0].shortcode == f"{DID}|3abc"


def test_get_post_accepts_composite_shortcode():
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("uris", ""))
        return httpx.Response(200, json={"posts": [_post()]})

    adapter = _scraper({"/xrpc/app.bsky.feed.getPosts": route})
    post = Post.model_validate(adapter.get_post(f"{DID}|3abc"))

    assert post.id == POST_URI
    assert seen == [POST_URI]


def test_get_post_rejects_invalid_shortcode():
    adapter = _scraper({})
    with pytest.raises(APIError) as error:
        adapter.get_post("3abc")
    assert error.value.status_code == 422
    assert error.value.code == "VALIDATION_ERROR"


def test_get_comments_flattens_thread():
    thread = {
        "thread": {
            "post": _post(),
            "replies": [
                {
                    "post": _post(uri=POST_URI + "/r1", text="uno", replies=0),
                    "replies": [
                        {
                            "post": _post(
                                uri=POST_URI + "/r2", text="dos", replies=0
                            ),
                            "replies": [],
                        }
                    ],
                }
            ],
        }
    }
    adapter = _scraper({"/xrpc/app.bsky.feed.getPostThread": thread})

    comments = [Comment.model_validate(c) for c in adapter.get_comments(f"{DID}|3abc")]
    assert [comment.text for comment in comments] == ["uno", "dos"]


def test_get_likes_and_follows_map_user_summary():
    actor = _profile()
    routes = {
        "/xrpc/app.bsky.feed.getLikes": {"likes": [{"actor": actor}]},
        "/xrpc/app.bsky.graph.getFollowers": {"followers": [actor]},
        "/xrpc/app.bsky.graph.getFollows": {"follows": [actor]},
    }
    adapter = _scraper(routes)

    likes = [UserSummary.model_validate(u) for u in adapter.get_likes(f"{DID}|3abc")]
    followers = [UserSummary.model_validate(u) for u in adapter.get_followers(HANDLE)]
    following = [UserSummary.model_validate(u) for u in adapter.get_following(HANDLE)]

    assert likes[0].username == HANDLE
    assert followers[0].user_id == DID
    assert following[0].username == HANDLE


def test_stories_and_highlights_are_empty():
    adapter = _scraper({})
    assert adapter.get_stories(HANDLE) == []
    assert adapter.get_highlights(HANDLE) == []


# ---------------------------------------------------------------------------
# Búsqueda y autenticación opcional
# ---------------------------------------------------------------------------
def test_hashtag_search_uses_pds_session_when_configured():
    seen: list[tuple[str, str | None]] = []

    def search_route(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.host, request.headers.get("authorization"))
        )
        return httpx.Response(
            200,
            json={"posts": [_post(text="#python y #api")]},
        )

    routes = {
        "/xrpc/com.atproto.server.createSession": {"accessJwt": "jwt-1"},
        "/xrpc/app.bsky.feed.searchPosts": search_route,
    }
    adapter = _scraper(
        routes, handle=HANDLE, app_password="app-password"
    )

    hashtag = Hashtag.model_validate(adapter.get_hashtag("python"))

    assert hashtag.name == "python"
    assert hashtag.media_count == 1
    assert hashtag.top_posts[0].shortcode == f"{DID}|3abc"
    assert seen == [("pds.test", "Bearer jwt-1")]


def test_hashtag_search_without_credentials_maps_403_to_upstream_error():
    adapter = _scraper(
        {
            "/xrpc/app.bsky.feed.searchPosts": httpx.Response(
                403, json={"error": "Forbidden", "message": "auth required"}
            )
        }
    )
    with pytest.raises(APIError) as error:
        adapter.get_hashtag_posts("python")
    assert error.value.status_code == 503
    assert error.value.code == "UPSTREAM_UNAVAILABLE"


def test_public_search_does_not_follow_blocked_cursor():
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"posts": [_post()], "cursor": "siguiente"}
        )

    adapter = _scraper({"/xrpc/app.bsky.feed.searchPosts": route})
    posts = adapter.get_hashtag_posts("python", count=120)

    assert len(posts) == 1
    assert calls["n"] == 1


def test_search_returns_users_and_hashtags():
    routes = {
        "/xrpc/com.atproto.server.createSession": {"accessJwt": "jwt-1"},
        "/xrpc/app.bsky.actor.searchActors": {
            "actors": [_profile(handle="alice.dev", did="did:plc:dev")]
        },
        "/xrpc/app.bsky.feed.searchPosts": {
            "posts": [_post(text="#python #python #api")]
        },
    }
    adapter = _scraper(routes, handle=HANDLE, app_password="app-password")

    results = adapter.search("python", kind="all", count=10)
    validated = [TypeAdapter(SearchResult).validate_python(item) for item in results]

    assert validated[0].kind == "user"
    assert validated[0].username == "alice.dev"
    tags = {item.name: item.media_count for item in validated if item.kind == "hashtag"}
    assert tags == {"python": 2, "api": 1}


def test_search_all_degrades_when_post_search_is_forbidden():
    routes = {
        "/xrpc/app.bsky.actor.searchActors": {"actors": [_profile()]},
        "/xrpc/app.bsky.feed.searchPosts": httpx.Response(403, json={}),
    }
    adapter = _scraper(routes)

    results = adapter.search("alice", kind="all", count=10)
    assert len(results) == 1
    assert results[0]["kind"] == "user"


# ---------------------------------------------------------------------------
# Errores, reintentos y fachada
# ---------------------------------------------------------------------------
def test_rate_limit_is_retried_then_succeeds():
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_profile())

    adapter = _scraper(
        {"/xrpc/app.bsky.actor.getProfile": route}, max_retries=2
    )
    assert adapter.get_profile(HANDLE)["username"] == HANDLE
    assert calls["n"] == 2


def test_rate_limit_exhausted_returns_api_error():
    adapter = _scraper(
        {
            "/xrpc/app.bsky.actor.getProfile": httpx.Response(
                429, headers={"Retry-After": "0"}
            )
        }
    )
    with pytest.raises(APIError) as error:
        adapter.get_profile(HANDLE)
    assert error.value.status_code == 429
    assert error.value.code == "RATE_LIMITED"
    assert error.value.headers["Retry-After"] == "0"


def test_not_found_is_mapped_from_400():
    adapter = _scraper(
        {
            "/xrpc/app.bsky.actor.getProfile": httpx.Response(
                400, json={"message": "Profile not found"}
            )
        }
    )
    with pytest.raises(APIError) as error:
        adapter.get_profile("nadie.test")
    assert error.value.status_code == 404
    assert error.value.code == "NOT_FOUND"


def test_network_error_is_mapped_to_upstream_unavailable():
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    adapter = _scraper({"/xrpc/app.bsky.actor.getProfile": route})
    with pytest.raises(APIError) as error:
        adapter.get_profile(HANDLE)
    assert error.value.status_code == 503
    assert error.value.code == "UPSTREAM_UNAVAILABLE"


def test_build_result_dispatches_to_backend_methods():
    routes = {
        "/xrpc/app.bsky.actor.getProfile": _profile(),
        "/xrpc/app.bsky.graph.getFollowers": {"followers": [_profile()]},
    }
    adapter = _scraper(routes)

    profile = adapter.build_result(
        "profile", {"username": HANDLE}, {"limit": 1}
    )
    followers = adapter.build_result(
        "profile_followers", {"username": HANDLE}, {"limit": 1}
    )

    assert Profile.model_validate(profile[0]).username == HANDLE
    assert UserSummary.model_validate(followers[0]).username == HANDLE
    assert adapter.build_result("desconocido", {}, {}) == []


def test_facade_selects_backend_and_can_reset(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scraper_backend", "bluesky")
    scraper.reset_backend()
    try:
        backend = scraper._get_backend()
        assert isinstance(backend, BlueskyScraper)
        backend.close()
    finally:
        scraper.reset_backend()

    monkeypatch.setattr(settings, "scraper_backend", "simulated")
    try:
        assert isinstance(scraper._get_backend(), SimulatedScraper)
    finally:
        scraper.reset_backend()


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def test_bluesky_credentials_must_be_configured_together():
    with pytest.raises(ValidationError):
        Settings(bluesky_handle="alice.bsky.social", bluesky_app_password=None)


def test_bluesky_base_urls_must_be_https():
    with pytest.raises(ValidationError):
        Settings(bluesky_api_base="http://public.api.bsky.app")


def test_simulated_backend_is_the_default():
    scraper.reset_backend()
    assert get_settings().scraper_backend == "simulated"
    payload = json.dumps(scraper.get_profile("nasa"), default=str)
    assert "Perfil simulado" in payload


def test_profile_cache_respects_ttl(client, auth_headers, monkeypatch):
    settings = get_settings()
    calls = {"n": 0}

    def fake_profile(username: str) -> dict:
        calls["n"] += 1
        return {
            "username": username,
            "user_id": "1",
            "full_name": "TTL",
            "biography": None,
            "is_private": False,
            "is_verified": False,
            "followers_count": calls["n"],
            "following_count": 0,
            "posts_count": 0,
            "profile_pic_url": None,
            "external_url": None,
            "scraped_at": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(scraper, "get_profile", fake_profile)
    monkeypatch.setattr(settings, "profile_cache_ttl_seconds", 3600)
    username = "ttl_" + uuid.uuid4().hex[:8]

    first = client.get(f"/api/v1/profiles/{username}", headers=auth_headers)
    second = client.get(f"/api/v1/profiles/{username}", headers=auth_headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["n"] == 1

    monkeypatch.setattr(settings, "profile_cache_ttl_seconds", 0)
    third = client.get(f"/api/v1/profiles/{username}", headers=auth_headers)
    assert third.status_code == 200
    assert calls["n"] == 2
