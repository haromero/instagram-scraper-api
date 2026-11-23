"""Cifrado simétrico de secretos en reposo (Fernet: AES + HMAC)."""

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from .config import get_settings
from .errors import APIError

# Prefijo de un token Fernet; sirve para distinguir datos ya cifrados de datos
# heredados en texto plano durante la migración.
_FERNET_PREFIX = "gAAAAA"


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(value: str) -> str:
    """Cifra un valor para almacenarlo en la base de datos."""
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str:
    """Descifra un valor almacenado.

    Si el valor no es un token Fernet se devuelve tal cual: permite arrancar
    antes de ejecutar ``scripts/migrate_secrets.py`` sin romper la lectura.
    """
    if not value.startswith(_FERNET_PREFIX):
        return value
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise APIError(
            500,
            "DECRYPTION_ERROR",
            "No se pudo descifrar un secreto almacenado.",
        ) from exc


def encrypt_optional(value: str | None) -> str | None:
    return encrypt(value) if value else None


def decrypt_optional(value: str | None) -> str | None:
    return decrypt(value) if value else None


class EncryptedText(TypeDecorator):
    """Columna de texto que cifra al escribir y descifra al leer."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return encrypt(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return decrypt(str(value))
