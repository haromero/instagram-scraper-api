"""Verificaciones de seguridad en vivo contra un servidor en marcha.

Uso:
    $env:IG_SMOKE_CLIENT_ID="cli_..."
    $env:IG_SMOKE_CLIENT_SECRET="..."
    $env:IG_SMOKE_ADMIN_ID="cli_platform"
    $env:IG_SMOKE_ADMIN_SECRET="..."
    python scripts/live_security_checks.py

Comprueba el *resultado observable* de cada control, no solo que responda.
El chequeo de rate limit de autenticación va al final porque consume el bucket
de la IP durante 60 s.
"""

import json
import os
import sqlite3
import sys
import time

import httpx

BASE_URL = os.getenv("IG_SMOKE_BASE_URL", "http://127.0.0.1:8000")
CLIENT_ID = os.getenv("IG_SMOKE_CLIENT_ID")
CLIENT_SECRET = os.getenv("IG_SMOKE_CLIENT_SECRET")
ADMIN_ID = os.getenv("IG_SMOKE_ADMIN_ID")
ADMIN_SECRET = os.getenv("IG_SMOKE_ADMIN_SECRET")
DB_PATH = os.getenv("IG_SMOKE_DB", "igscraper.db")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return ok


def expect(name: str, response: httpx.Response, status: int) -> bool:
    ok = response.status_code == status
    detail = str(response.status_code)
    if not ok:
        detail += " " + response.text[:300]
    return record(name, ok, detail)


def wait_ready(client: httpx.Client, attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            if client.get("/api/v1/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def login(client: httpx.Client, client_id: str, secret: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token",
        json={"client_id": client_id, "client_secret": secret},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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

    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        if not wait_ready(client):
            print("El servidor no respondió a /health.")
            return 3

        client_headers = login(client, CLIENT_ID, CLIENT_SECRET)
        admin_headers = login(client, ADMIN_ID, ADMIN_SECRET)

        # --- 1. Autorización por scope -------------------------------------
        expect(
            "token normal -> /admin/sessions = 403",
            client.get("/api/v1/admin/sessions", headers=client_headers),
            403,
        )
        expect(
            "token admin -> /admin/sessions = 200",
            client.get("/api/v1/admin/sessions", headers=admin_headers),
            200,
        )

        # --- 2. SSRF --------------------------------------------------------
        ssrf = client.post(
            "/api/v1/webhooks",
            headers=client_headers,
            json={"url": "http://localtest.me/hook", "events": ["job.completed"]},
        )
        expect("SSRF localtest.me -> 422", ssrf, 422)

        # --- 3. Cabeceras y request id en errores ---------------------------
        unauthorized = client.get("/api/v1/profiles/nasa")
        record(
            "error 401 conserva cabeceras de seguridad",
            unauthorized.status_code == 401
            and unauthorized.headers.get("X-Content-Type-Options") == "nosniff"
            and unauthorized.headers.get("X-Frame-Options") == "DENY"
            and unauthorized.headers.get("Cache-Control") == "no-store",
            str(dict(unauthorized.headers)),
        )
        custom_id = "req-live-check-1234"
        with_id = client.get(
            "/api/v1/profiles/nasa", headers={"X-Request-ID": custom_id}
        )
        record(
            "X-Request-ID se propaga a cabecera y envelope",
            with_id.headers.get("X-Request-ID") == custom_id
            and with_id.json()["error"]["request_id"] == custom_id,
            with_id.headers.get("X-Request-ID", ""),
        )

        # --- 4. Límite de body ---------------------------------------------
        big = client.post(
            "/api/v1/webhooks",
            headers={**client_headers, "Content-Type": "application/json"},
            content=b"x" * 1_100_000,
        )
        record(
            "body > 1 MB -> 413 con cabeceras de seguridad y rate limit",
            big.status_code == 413
            and big.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
            and big.headers.get("X-Content-Type-Options") == "nosniff"
            and "X-RateLimit-Limit" in big.headers,
            f"{big.status_code} {big.text[:120]}",
        )

        # --- 5. Cifrado en reposo ------------------------------------------
        marker = f"live-{int(time.time())}"
        hook = client.post(
            "/api/v1/webhooks",
            headers=client_headers,
            json={
                "url": f"https://example.com/hook?token={marker}",
                "events": ["job.completed"],
            },
        )
        proxy_marker = f"liveuser{int(time.time())}"
        proxy = client.post(
            "/api/v1/admin/proxies",
            headers=admin_headers,
            json={"url": f"http://{proxy_marker}:clave@8.8.8.8:8080"},
        )
        if hook.status_code != 201 or proxy.status_code != 201:
            record("crear webhook y proxy para verificar cifrado", False,
                   f"{hook.status_code}/{proxy.status_code}")
        else:
            hook_id = hook.json()["webhook_id"]
            proxy_id = proxy.json()["proxy_id"]
            connection = sqlite3.connect(DB_PATH)
            try:
                stored_url = connection.execute(
                    "SELECT url FROM webhooks WHERE id = ?", (hook_id,)
                ).fetchone()[0]
                stored_user = connection.execute(
                    "SELECT username FROM proxies WHERE id = ?", (proxy_id,)
                ).fetchone()[0]
            finally:
                connection.close()
            record(
                "webhook.url cifrada en reposo (Fernet)",
                str(stored_url).startswith("gAAAAA") and marker not in str(stored_url),
                str(stored_url)[:14],
            )
            record(
                "proxy.username cifrado en reposo (Fernet)",
                str(stored_user).startswith("gAAAAA")
                and proxy_marker not in str(stored_user),
                str(stored_user)[:14],
            )
            # Limpieza
            client.delete(f"/api/v1/webhooks/{hook_id}", headers=client_headers)

        # --- 6. Docs con IG_DEBUG=true -------------------------------------
        docs = client.get("/docs")
        expect("IG_DEBUG=true -> /docs = 200", docs, 200)

        # --- 7. Rate limit de autenticación (último) ------------------------
        codes = []
        retry_after = None
        for _ in range(12):
            response = client.post(
                "/api/v1/auth/token",
                json={"client_id": CLIENT_ID, "client_secret": "incorrecto"},
            )
            codes.append(response.status_code)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                break
        record(
            "auth rate limit -> 429 con Retry-After",
            429 in codes and retry_after is not None,
            f"codes={codes} retry_after={retry_after}",
        )

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} comprobaciones de seguridad OK")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
