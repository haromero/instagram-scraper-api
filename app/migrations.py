"""Migraciones ligeras para bases de datos SQLite ya existentes.

No sustituye a Alembic: solo añade las columnas nuevas que introduce el
hardening para que ``create_all`` no deje esquemas a medias en desarrollo.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .config import get_settings

logger = logging.getLogger(__name__)

_ADDED_COLUMNS = {
    "sessions": {"owner": "VARCHAR"},
    "proxies": {"owner": "VARCHAR"},
}


def run_startup_migrations(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    default_owner = get_settings().client_id

    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            current = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name in current:
                    continue
                logger.info("Añadiendo columna %s.%s", table, name)
                connection.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl_type}')
                )
                connection.execute(
                    text(
                        f'UPDATE "{table}" SET "{name}" = :owner '
                        f'WHERE "{name}" IS NULL'
                    ),
                    {"owner": default_owner},
                )
