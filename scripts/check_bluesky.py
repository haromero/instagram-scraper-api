r"""Comprobación del adaptador de Bluesky contra la API real (sin mocks).

Uso:
    .\.venv\Scripts\python.exe scripts\check_bluesky.py
    .\.venv\Scripts\python.exe scripts\check_bluesky.py --actor bsky.app

No necesita credenciales para perfiles, posts, seguidores, follows, hilos ni
likes. La búsqueda de posts solo se prueba si defines
``IG_BLUESKY_HANDLE`` e ``IG_BLUESKY_APP_PASSWORD`` en el entorno.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scraper_bluesky import BlueskyScraper  # noqa: E402


def _ok(label: str, detail: str = "") -> None:
    print(f"[OK]   {label}" + (f": {detail}" if detail else ""))


def _fail(label: str, error: Exception) -> None:
    print(f"[FAIL] {label}: {type(error).__name__}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actor",
        default=os.getenv("IG_BLUESKY_TEST_ACTOR", "bsky.app"),
        help="Handle o DID de Bluesky para las pruebas (default: bsky.app)",
    )
    args = parser.parse_args()

    adapter = BlueskyScraper(
        api_base=os.getenv("IG_BLUESKY_API_BASE", "https://public.api.bsky.app"),
        appview_base=os.getenv(
            "IG_BLUESKY_APPVIEW_BASE", "https://api.bsky.app"
        ),
        pds_url=os.getenv("IG_BLUESKY_PDS_URL", "https://bsky.social"),
        handle=os.getenv("IG_BLUESKY_HANDLE") or None,
        app_password=os.getenv("IG_BLUESKY_APP_PASSWORD") or None,
        timeout=30.0,
        max_retries=2,
        backoff_seconds=1.0,
    )

    failures = 0

    try:
        profile = adapter.get_profile(args.actor)
        _ok(
            "perfil",
            f"@{profile['username']} ({profile['user_id']}), "
            f"{profile['followers_count']} seguidores",
        )
    except Exception as exc:  # noqa: BLE001 - script de diagnóstico
        _fail("perfil", exc)
        failures += 1

    posts: list[dict] = []
    try:
        posts = adapter.get_posts(args.actor, count=5)
        _ok("posts", f"{len(posts)} publicaciones")
    except Exception as exc:  # noqa: BLE001
        _fail("posts", exc)
        failures += 1

    try:
        followers = adapter.get_followers(args.actor, count=3)
        _ok("followers", f"{len(followers)} cuentas")
    except Exception as exc:  # noqa: BLE001
        _fail("followers", exc)
        failures += 1

    try:
        following = adapter.get_following(args.actor, count=3)
        _ok("following", f"{len(following)} cuentas")
    except Exception as exc:  # noqa: BLE001
        _fail("following", exc)
        failures += 1

    if posts:
        shortcode = posts[0]["shortcode"]
        try:
            comments = adapter.get_comments(shortcode, count=3)
            _ok("comentarios", f"{len(comments)} respuestas del primer post")
        except Exception as exc:  # noqa: BLE001
            _fail("comentarios", exc)
            failures += 1

        try:
            likes = adapter.get_likes(shortcode, count=3)
            _ok("likes", f"{len(likes)} cuentas")
        except Exception as exc:  # noqa: BLE001
            _fail("likes", exc)
            failures += 1
    else:
        print("[SKIP] comentarios/likes: no hay posts para probar")

    try:
        actors = adapter.search("bluesky", kind="user", count=3)
        _ok("searchActors", f"{len(actors)} resultados")
    except Exception as exc:  # noqa: BLE001
        _fail("searchActors", exc)
        failures += 1

    try:
        hashtag = adapter.get_hashtag("bluesky")
        _ok(
            "hashtags/searchPosts",
            f"#{hashtag['name']}, {hashtag['media_count']} posts",
        )
    except Exception as exc:  # noqa: BLE001
        _fail("hashtags/searchPosts", exc)
        failures += 1

    if not (os.getenv("IG_BLUESKY_HANDLE") and os.getenv("IG_BLUESKY_APP_PASSWORD")):
        print(
            "[INFO] Sin credenciales: searchPosts usa el AppView público "
            "(api.bsky.app). Configura IG_BLUESKY_HANDLE e "
            "IG_BLUESKY_APP_PASSWORD para usar tu PDS."
        )

    adapter.close()
    print()
    if failures:
        print(f"{failures} comprobación(es) fallaron.")
        return 1
    print("Todas las comprobaciones pasaron.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
