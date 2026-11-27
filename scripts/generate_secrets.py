"""Genera un set completo de secretos para desplegar la API.

Uso:
    python scripts/generate_secrets.py
    python scripts/generate_secrets.py --out C:\\ruta\\secretos.txt

Imprime (o escribe) los valores listos para pegar en el Blueprint de Render.
No guarda nada en el repositorio. Los secretos generados invalidan los
anteriores: hay que actualizarlos en Render y volver a desplegar.
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet  # noqa: E402

from app.passwords import hash_secret, verify_secret  # noqa: E402

ORDER = (
    "IG_ENCRYPTION_KEY",
    "IG_JWT_SECRET",
    "CLIENT_SECRET",
    "IG_CLIENT_SECRET_HASH",
    "ADMIN_SECRET",
    "IG_ADMIN_CLIENT_SECRET_HASH",
)


def build_secrets() -> dict[str, str]:
    """Crea un set nuevo y verifica que los hashes coinciden con los secretos."""
    encryption_key = Fernet.generate_key().decode()
    jwt_secret = secrets.token_urlsafe(48)
    client_secret = "igc_" + secrets.token_urlsafe(32)
    admin_secret = "iga_" + secrets.token_urlsafe(32)
    client_hash = hash_secret(client_secret)
    admin_hash = hash_secret(admin_secret)

    if not verify_secret(client_secret, client_hash):
        raise RuntimeError("El hash del cliente no verifica el secreto generado.")
    if not verify_secret(admin_secret, admin_hash):
        raise RuntimeError("El hash del admin no verifica el secreto generado.")

    return {
        "IG_ENCRYPTION_KEY": encryption_key,
        "IG_JWT_SECRET": jwt_secret,
        "CLIENT_SECRET": client_secret,
        "IG_CLIENT_SECRET_HASH": client_hash,
        "ADMIN_SECRET": admin_secret,
        "IG_ADMIN_CLIENT_SECRET_HASH": admin_hash,
    }


def format_secrets(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={values[key]}" for key in ORDER) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="Escribe los secretos en este archivo en vez de imprimirlos.",
    )
    args = parser.parse_args()

    content = format_secrets(build_secrets())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(content, encoding="utf-8")
        print(f"Secretos escritos en {args.out}")
        return
    print(content, end="")


if __name__ == "__main__":
    main()
