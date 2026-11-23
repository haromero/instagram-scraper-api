"""Genera el hash PBKDF2 de un secreto de cliente.

Uso:
    python scripts/hash_secret.py
    python scripts/hash_secret.py "mi-secreto"
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.passwords import hash_secret  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        secret = sys.argv[1]
    else:
        secret = getpass.getpass("Secreto: ")
    if len(secret) < 12:
        raise SystemExit("El secreto debe tener al menos 12 caracteres.")
    print(hash_secret(secret))


if __name__ == "__main__":
    main()
