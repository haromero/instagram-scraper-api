"""Endpoint de autenticación: intercambio de credenciales por JWT."""

from fastapi import APIRouter

from ..audit import log_event
from ..config import get_settings
from ..dependencies import constant_time_equals
from ..errors import APIError
from ..passwords import DUMMY_HASH, verify_secret
from ..schemas import AuthTokenRequest, AuthTokenResponse
from ..security import create_access_token

router = APIRouter(tags=["Auth"])


def _resolve_client(client_id: str, client_secret: str) -> tuple[list[str], bool]:
    """Devuelve los scopes del cliente o lanza 401.

    Se verifica siempre un hash (real o de relleno) para que el tiempo de
    respuesta no revele si el client_id existe.
    """
    settings = get_settings()

    stored_hash: str | None = None
    scopes: list[str] = []
    is_admin = False

    if settings.admin_client_id and constant_time_equals(client_id, settings.admin_client_id):
        stored_hash = settings.admin_client_secret_hash
        scopes = settings.admin_scopes_list
        is_admin = True
    elif constant_time_equals(client_id, settings.client_id):
        stored_hash = settings.client_secret_hash
        scopes = settings.client_scopes_list

    if not verify_secret(client_secret, stored_hash or DUMMY_HASH) or stored_hash is None:
        raise APIError(
            401,
            "INVALID_CLIENT",
            "client_id o client_secret inválidos.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return scopes, is_admin


@router.post(
    "/auth/token",
    response_model=AuthTokenResponse,
    summary="Obtener access token",
    description="Intercambia client_id y client_secret por un token Bearer.",
)
def create_token(body: AuthTokenRequest) -> AuthTokenResponse:
    try:
        scopes, is_admin = _resolve_client(body.client_id, body.client_secret)
    except APIError:
        log_event(
            "auth.login",
            actor=body.client_id[:64],
            outcome="failure",
            reason="invalid_client",
        )
        raise

    token, expires_in = create_access_token(body.client_id, scopes)
    log_event(
        "auth.login",
        actor=body.client_id,
        outcome="success",
        scopes=scopes,
        admin=is_admin,
    )
    return AuthTokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=expires_in,
        scopes=scopes,
    )
