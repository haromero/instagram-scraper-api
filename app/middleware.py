"""Middlewares: request_id, rate limit, límite de body y cabeceras de seguridad."""

import ipaddress
import re
import uuid

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings
from .context import get_request_id, reset_request_id, set_request_id
from .ratelimit import RateLimiter, build_rate_limiter
from .security import decode_token

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def is_valid_request_id(value: str) -> bool:
    """Valida la cabecera completa: ``$`` aceptaría un LF final."""
    return bool(_REQUEST_ID_RE.fullmatch(value))


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Genera (o acepta) un request_id y lo expone en la respuesta."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        if is_valid_request_id(incoming):
            request_id = incoming
        else:
            request_id = "req_" + uuid.uuid4().hex

        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["X-Request-ID"] = request_id
        return response


class _BodyTooLarge(Exception):
    """Se lanza desde ``receive`` cuando el body excede el límite."""


class MaxBodySizeMiddleware:
    """Rechaza cuerpos mayores que ``IG_MAX_BODY_BYTES``.

    Es ASGI puro para cubrir también bodies ``Transfer-Encoding: chunked`` que
    no traen ``Content-Length``.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.max_bytes = get_settings().max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError:
                length = 0
            if length > self.max_bytes:
                await self._reject(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send) -> None:
        await _error_response(
            413,
            "PAYLOAD_TOO_LARGE",
            "El cuerpo de la petición es demasiado grande.",
            {"max_bytes": self.max_bytes},
        )(scope, receive, send)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Añade cabeceras de seguridad a todas las respuestas."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https" or get_settings().force_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limit de ventana fija por token (o IP si no hay token).

    Los contadores viven en el ``RateLimiter`` inyectado: en memoria por
    defecto o en Redis cuando ``IG_RATE_LIMIT_BACKEND=redis``. Así varias
    réplicas comparten el mismo límite sin cambiar el contrato HTTP.
    """

    def __init__(self, app, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        settings = get_settings()
        self.default_limit = max(1, settings.rate_limit_per_minute)
        self.auth_limit = max(1, settings.auth_rate_limit_per_minute)
        self.jobs_limit = max(1, settings.jobs_rate_limit_per_minute)
        self.ready_limit = max(1, settings.health_ready_rate_limit_per_minute)
        self.window = 60.0
        self.limiter = limiter or build_rate_limiter(settings)
        self._trusted_proxies = [
            ipaddress.ip_network(entry, strict=False)
            for entry in settings.trusted_proxies_list
        ]

    def _is_trusted(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self._trusted_proxies)

    def _client_ip(self, request: Request) -> str:
        host = request.client.host if request.client else "unknown"
        if not self._is_trusted(host):
            return host
        forwarded = request.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        # Se recorre de derecha a izquierda: el primer salto no confiable es el
        # cliente real. La cabecera la puede inventar cualquiera, pero solo se
        # lee cuando el peer es un proxy configurado explícitamente.
        for hop in reversed(hops):
            if not self._is_trusted(hop):
                return hop
        return hops[0] if hops else host

    def _client_key(self, request: Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            try:
                payload = decode_token(header[7:].strip())
                sub = str(payload.get("sub", "unknown"))
                tenant = str(payload.get("tenant") or "")
                return f"sub:{sub}:tenant:{tenant}"
            except jwt.PyJWTError:
                pass
        return "ip:" + self._client_ip(request)

    def _bucket_for(self, request: Request) -> tuple[str, int]:
        """Devuelve (tipo de cubo, límite), independientes del valor numérico.

        Usar el número como sufijo hacía que general y jobs compartieran clave
        cuando ambos límites coincidían (configuración válida).
        """
        path = request.url.path
        if path.endswith("/auth/token"):
            return "auth", self.auth_limit
        if path.endswith("/health/ready"):
            return "ready", self.ready_limit
        if path == "/api/v1/jobs" and request.method == "POST":
            return "jobs", self.jobs_limit
        return "general", self.default_limit

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in (
            "/api/v1/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
        ):
            return await call_next(request)

        key = self._client_key(request)
        bucket, limit = self._bucket_for(request)
        critical = path.endswith("/auth/token")
        # El sufijo es el tipo de cubo: así un mismo límite numérico en general
        # y jobs no comparte contador. El hash tag de Redis se construye solo
        # con la clave del cliente (ver ratelimit.py).
        result = self.limiter.hit(f"{key}:{bucket}", limit, self.window, critical=critical)

        if not result.allowed:
            payload = {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Has excedido el límite de peticiones.",
                    "request_id": get_request_id(),
                    "details": {
                        "retry_after_seconds": result.retry_after,
                        "limit": result.limit,
                    },
                }
            }
            return JSONResponse(
                status_code=429,
                content=payload,
                headers={
                    "Retry-After": str(result.retry_after),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(result.reset_at),
                    "X-RateLimit-Degraded": "1" if result.degraded else "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(result.reset_at)
        if result.degraded:
            response.headers["X-RateLimit-Degraded"] = "1"
        return response


def _error_response(status_code: int, code: str, message: str, details: dict) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": get_request_id(),
                "details": details,
            }
        },
    )
