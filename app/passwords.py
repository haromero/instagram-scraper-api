"""Hash y verificación de secretos de cliente.

Usa PBKDF2-HMAC-SHA256 de la librería estándar: no añade dependencias con
extensiones compiladas (que en este equipo bloquea el Control de aplicaciones)
y permite comparación en tiempo constante.
"""

import base64
import binascii
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000
# Cota inferior y superior: evita hashes débiles y que un valor manipulado en
# la configuración bloquee la CPU con millones de iteraciones.
_MIN_ITERATIONS = 100_000
_MAX_ITERATIONS = 2_000_000

# Hash de relleno para igualar el tiempo de respuesta cuando el client_id no
# existe y evitar enumeración de clientes por temporización.
DUMMY_HASH = (
    "pbkdf2_sha256$600000$AAAAAAAAAAAAAAAAAAAAAA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


def hash_secret(secret: str, *, iterations: int = _ITERATIONS) -> str:
    """Devuelve ``pbkdf2_sha256$<iteraciones>$<salt_b64>$<hash_b64>``."""
    if not _MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS:
        raise ValueError(
            f"Las iteraciones deben estar entre {_MIN_ITERATIONS} y "
            f"{_MAX_ITERATIONS}."
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return (
        f"{_ALGORITHM}${iterations}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )


def verify_secret(secret: str, stored: str) -> bool:
    """Verifica un secreto contra el hash almacenado en tiempo constante."""
    try:
        algorithm, iterations_raw, salt_b64, digest_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_raw)
        if not _MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        if not salt or not expected:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError, binascii.Error):
        return False
