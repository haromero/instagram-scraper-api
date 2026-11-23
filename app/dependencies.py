"""Dependencias de autenticación y autorización por scope."""

import hmac

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ADMIN_SCOPE, CLIENT_SCOPES
from .errors import APIError
from .security import decode_token

bearer_scheme = HTTPBearer(auto_error=False)

ADMIN_SCOPES = [*CLIENT_SCOPES, ADMIN_SCOPE]

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_current_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    if credentials is None:
        raise APIError(
            401,
            "UNAUTHORIZED",
            "Token ausente o inválido.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise APIError(
            401,
            "UNAUTHORIZED",
            "Token ausente o inválido.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    if not payload.get("sub"):
        raise APIError(
            401,
            "UNAUTHORIZED",
            "Token ausente o inválido.",
            headers=_UNAUTHORIZED_HEADERS,
        )

    # Disponible para middlewares/endpoints que necesiten el tenant sin volver
    # a decodificar el token.
    request.state.client_sub = payload["sub"]
    request.state.tenant = payload.get("tenant") or payload["sub"]
    request.state.scopes = payload.get("scopes", [])
    return payload


def require_scope(scope: str):
    def _checker(client: dict = Depends(get_current_client)) -> dict:
        if scope not in client.get("scopes", []):
            raise APIError(
                403,
                "FORBIDDEN",
                "El token no tiene el scope requerido.",
                {"required_scope": scope},
            )
        return client

    return _checker


def has_scope(client: dict, scope: str) -> bool:
    return scope in client.get("scopes", [])


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
