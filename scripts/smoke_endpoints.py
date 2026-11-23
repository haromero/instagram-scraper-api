"""Smoke test de los 30 endpoints contra un servidor en marcha.

Uso:
    $env:IG_SMOKE_CLIENT_ID="cli_..."
    $env:IG_SMOKE_CLIENT_SECRET="..."
    $env:IG_SMOKE_ADMIN_ID="cli_platform"
    $env:IG_SMOKE_ADMIN_SECRET="..."
    python scripts/smoke_endpoints.py

Las credenciales no se guardan en el repositorio: se leen del entorno.
"""

import json
import os
import sys
import time
import uuid

import httpx

BASE_URL = os.getenv("IG_SMOKE_BASE_URL", "http://127.0.0.1:8000")
CLIENT_ID = os.getenv("IG_SMOKE_CLIENT_ID")
CLIENT_SECRET = os.getenv("IG_SMOKE_CLIENT_SECRET")
ADMIN_ID = os.getenv("IG_SMOKE_ADMIN_ID")
ADMIN_SECRET = os.getenv("IG_SMOKE_ADMIN_SECRET")

results: list[tuple[str, bool, str]] = []


def check(name: str, response: httpx.Response, expected: int | set[int]) -> httpx.Response:
    allowed = {expected} if isinstance(expected, int) else expected
    ok = response.status_code in allowed
    detail = f"{response.status_code}"
    if not ok:
        detail += " " + response.text[:200]
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return response


def login(client: httpx.Client, client_id: str, secret: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token",
        json={"client_id": client_id, "client_secret": secret},
    )
    check("POST /auth/token", response, 200)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def wait_completed(
    client: httpx.Client, headers: dict[str, str], job_id: str, timeout: float = 20.0
) -> httpx.Response:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        if last.status_code == 200 and last.json()["status"] in (
            "completed",
            "failed",
            "cancelled",
        ):
            return last
        time.sleep(0.2)
    assert last is not None
    return last


def main() -> int:
    missing = [
        name
        for name, value in (
            ("IG_SMOKE_CLIENT_ID", CLIENT_ID),
            ("IG_SMOKE_CLIENT_SECRET", CLIENT_SECRET),
            ("IG_SMOKE_ADMIN_ID", ADMIN_ID),
            ("IG_SMOKE_ADMIN_SECRET", ADMIN_SECRET),
        )
        if not value
    ]
    if missing:
        print("Faltan variables de entorno: " + ", ".join(missing))
        return 2

    suffix = uuid.uuid4().hex[:8]
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        client_headers = login(client, CLIENT_ID, CLIENT_SECRET)
        admin_headers = login(client, ADMIN_ID, ADMIN_SECRET)

        # 2. Crear job de posts (se espera a que termine para probar el resultado).
        created = check(
            "POST /jobs",
            client.post(
                "/api/v1/jobs",
                headers=client_headers,
                json={
                    "type": "profile_posts",
                    "target": {"username": "nasa"},
                    "options": {"limit": 5},
                },
            ),
            202,
        )
        job_id = created.json()["job_id"]

        # 3. Listar jobs.
        check("GET /jobs", client.get("/api/v1/jobs", headers=client_headers), 200)

        # 4. Consultar job.
        check(
            "GET /jobs/{job_id}",
            client.get(f"/api/v1/jobs/{job_id}", headers=client_headers),
            200,
        )

        # 5. Resultado (solo cuando completed).
        finished = wait_completed(client, client_headers, job_id)
        assert finished.json()["status"] == "completed", finished.text
        check(
            "GET /jobs/{job_id}/result",
            client.get(f"/api/v1/jobs/{job_id}/result", headers=client_headers),
            200,
        )

        # 6. Cancelar un segundo job.
        cancellable = client.post(
            "/api/v1/jobs",
            headers=client_headers,
            json={"type": "profile", "target": {"username": "nasa"}},
        )
        check("POST /jobs (cancelable)", cancellable, 202)
        check(
            "DELETE /jobs/{job_id}",
            client.delete(
                f"/api/v1/jobs/{cancellable.json()['job_id']}",
                headers=client_headers,
            ),
            200,
        )

        # 7-12. Perfiles.
        for name, path in (
            ("GET /profiles/{username}", "/api/v1/profiles/nasa"),
            ("GET /profiles/{username}/posts", "/api/v1/profiles/nasa/posts"),
            ("GET /profiles/{username}/followers", "/api/v1/profiles/nasa/followers"),
            ("GET /profiles/{username}/following", "/api/v1/profiles/nasa/following"),
            ("GET /profiles/{username}/stories", "/api/v1/profiles/nasa/stories"),
            ("GET /profiles/{username}/highlights", "/api/v1/profiles/nasa/highlights"),
        ):
            check(name, client.get(path, headers=client_headers), 200)

        # 13-15. Posts.
        for name, path in (
            ("GET /posts/{shortcode}", "/api/v1/posts/ABC123"),
            ("GET /posts/{shortcode}/comments", "/api/v1/posts/ABC123/comments"),
            ("GET /posts/{shortcode}/likes", "/api/v1/posts/ABC123/likes"),
        ):
            check(name, client.get(path, headers=client_headers), 200)

        # 16-17. Hashtags.
        check(
            "GET /hashtags/{hashtag}",
            client.get("/api/v1/hashtags/nasa", headers=client_headers),
            200,
        )
        check(
            "GET /hashtags/{hashtag}/posts",
            client.get("/api/v1/hashtags/nasa/posts", headers=client_headers),
            200,
        )

        # 18. Búsqueda.
        check(
            "GET /search",
            client.get(
                "/api/v1/search",
                headers=client_headers,
                params={"q": "nasa", "type": "all", "limit": 5},
            ),
            200,
        )

        # 19-21. Webhooks.
        hook = check(
            "POST /webhooks",
            client.post(
                "/api/v1/webhooks",
                headers=client_headers,
                json={
                    "url": f"https://example.com/hooks/{suffix}",
                    "events": ["job.completed"],
                },
            ),
            201,
        )
        check("GET /webhooks", client.get("/api/v1/webhooks", headers=client_headers), 200)
        check(
            "DELETE /webhooks/{webhook_id}",
            client.delete(
                f"/api/v1/webhooks/{hook.json()['webhook_id']}",
                headers=client_headers,
            ),
            204,
        )

        # 22-23. Cuenta.
        check(
            "GET /account/usage",
            client.get("/api/v1/account/usage", headers=client_headers),
            200,
        )
        check(
            "GET /account/limits",
            client.get("/api/v1/account/limits", headers=client_headers),
            200,
        )

        # 24-25. Health.
        check("GET /health", client.get("/api/v1/health"), 200)
        check("GET /health/ready", client.get("/api/v1/health/ready"), 200)

        # 26-28. Sesiones de admin.
        session = check(
            "POST /admin/sessions",
            client.post(
                "/api/v1/admin/sessions",
                headers=admin_headers,
                json={
                    "username": f"smoke_{suffix}",
                    "cookies": json.dumps(
                        [{"name": "sessionid", "value": "smoke-" + suffix}]
                    ),
                },
            ),
            201,
        )
        check(
            "GET /admin/sessions",
            client.get("/api/v1/admin/sessions", headers=admin_headers),
            200,
        )
        check(
            "DELETE /admin/sessions/{session_id}",
            client.delete(
                f"/api/v1/admin/sessions/{session.json()['session_id']}",
                headers=admin_headers,
            ),
            204,
        )

        # 29-30. Proxies de admin.
        check(
            "POST /admin/proxies",
            client.post(
                "/api/v1/admin/proxies",
                headers=admin_headers,
                json={"url": "http://8.8.8.8:8080", "country": "US"},
            ),
            201,
        )
        check(
            "GET /admin/proxies",
            client.get("/api/v1/admin/proxies", headers=admin_headers),
            200,
        )

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} comprobaciones OK")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
