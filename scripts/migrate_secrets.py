"""Cifra los secretos que quedaron en texto plano en una base de datos previa.

Uso (con el .env ya configurado):
    python scripts/migrate_secrets.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Proxy, Session, Webhook  # noqa: E402

_TARGETS = [
    (Session, "cookies"),
    (Proxy, "username"),
    (Proxy, "password"),
    (Webhook, "url"),
    (Webhook, "secret"),
]


def migrate(db) -> dict[str, int]:
    """Reescribe las columnas cifradas y devuelve cuántas filas tocó cada una.

    ``flag_modified`` es imprescindible: al leer, ``EncryptedText`` ya devuelve
    el valor descifrado, así que reasignar el mismo valor no marca la fila como
    sucia y SQLAlchemy no ejecutaría el UPDATE.
    """
    counts: dict[str, int] = {}
    for model, attribute in _TARGETS:
        migrated = 0
        for row in db.query(model).all():
            value = getattr(row, attribute)
            if not value:
                continue
            setattr(row, attribute, value)
            flag_modified(row, attribute)
            migrated += 1
        db.commit()
        counts[f"{model.__tablename__}.{attribute}"] = migrated
    return counts


def main() -> None:
    db = SessionLocal()
    try:
        for key, migrated in migrate(db).items():
            print(f"{key}: {migrated} registros cifrados")
    finally:
        db.close()


if __name__ == "__main__":
    main()
