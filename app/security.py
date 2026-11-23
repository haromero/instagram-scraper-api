"""Emisión y verificación de tokens JWT."""

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from . import revocation
from .config import get_settings

_MAX_CLAIM_LENGTH = 128
_MAX_SCOPES = 50
_MAX_SCOPE_LENGTH = 64


def create_access_token(
    client_id: str,
    scopes: list[str],
    *,
    tenant: str | None = None,
) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.jwt_expires_seconds)
    payload = {
        "sub": client_id,
        "tenant": tenant or client_id,
        "scopes": scopes,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": expires_at,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.jwt_expires_seconds


def decode_token(token: str) -> dict:
    """Valida firma, emisor, audiencia, expiración y claims obligatorios."""
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        leeway=settings.jwt_leeway_seconds,
        options={
            "require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"],
            "verify_aud": True,
            "verify_iss": True,
            "verify_exp": True,
            "verify_iat": True,
        },
    )
    _validate_claims(payload)
    if revocation.is_revoked(payload["jti"]):
        raise jwt.InvalidTokenError("Token revocado.")
    return payload


def _validate_claims(payload: dict) -> None:
    """Comprueba el tipo y tamaño de los claims antes de usarlos.

    PyJWT valida la firma y las fechas, pero no el tipo de los claims. Un token
    firmado con ``sub``/``jti``/``scopes`` de tipos inesperados podría provocar
    errores 500 o comparaciones ambiguas.
    """
    sub = payload.get("sub")
    if not isinstance(sub, str) or not 0 < len(sub) <= _MAX_CLAIM_LENGTH:
        raise jwt.InvalidTokenError("Claim 'sub' inválido.")

    jti = payload.get("jti")
    if not isinstance(jti, str) or not 0 < len(jti) <= _MAX_CLAIM_LENGTH:
        raise jwt.InvalidTokenError("Claim 'jti' inválido.")

    scopes = payload.get("scopes")
    if (
        not isinstance(scopes, list)
        or len(scopes) > _MAX_SCOPES
        or not all(
            isinstance(scope, str) and 0 < len(scope) <= _MAX_SCOPE_LENGTH
            for scope in scopes
        )
    ):
        raise jwt.InvalidTokenError("Claim 'scopes' inválido.")

    tenant = payload.get("tenant")
    if tenant is not None and (
        not isinstance(tenant, str) or not 0 < len(tenant) <= _MAX_CLAIM_LENGTH
    ):
        raise jwt.InvalidTokenError("Claim 'tenant' inválido.")


def revoke_token(token: str) -> None:
    """Revoca un token concreto (para un futuro endpoint de logout)."""
    payload = decode_token(token)
    revocation.revoke(payload["jti"], float(payload["exp"]))
