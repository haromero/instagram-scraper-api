"""Ejecución de migraciones Alembic en el arranque de la app y del worker.

Sustituye a ``Base.metadata.create_all``: el esquema vive en
``alembic/versions`` y Alembic mantiene la tabla ``alembic_version``. Las bases
SQLite creadas por versiones anteriores se "baselanean": se les añaden las
columnas nuevas y se marcan en ``head`` sin recrear las tablas.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from . import models  # noqa: F401  (registra todas las tablas en los metadatos)
from .config import get_settings
from .database import Base

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()
# Entero arbitrario y estable para serializar migraciones entre réplicas.
_ADVISORY_LOCK_KEY = 0x4967534352415045

_LEGACY_COLUMNS: dict[str, dict[str, tuple[str, object]]] = {
    "jobs": {"attempts": ("INTEGER NOT NULL DEFAULT 0", 0)},
    "sessions": {"owner": ("VARCHAR", None)},
    "proxies": {"owner": ("VARCHAR", None)},
}


def _build_config() -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    return config


def _add_legacy_columns(connection, inspector, tables: set[str]) -> None:
    owner = get_settings().client_id
    for table, columns in _LEGACY_COLUMNS.items():
        if table not in tables:
            continue
        current = {column["name"] for column in inspector.get_columns(table)}
        for name, (ddl_type, default) in columns.items():
            if name in current:
                continue
            logger.info("Añadiendo columna %s.%s antes de baselinar", table, name)
            connection.execute(
                text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl_type}')
            )
            value = owner if default is None else default
            connection.execute(
                text(f'UPDATE "{table}" SET "{name}" = :value WHERE "{name}" IS NULL'),
                {"value": value},
            )


def _create_missing_tables(connection, tables: set[str]) -> None:
    """En un baseline añade tablas nuevas que la BD heredada nunca tuvo."""
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            logger.info("Creando tabla nueva %s antes de baselinar", table.name)
            table.create(bind=connection, checkfirst=True)


def upgrade_database(engine: Engine) -> None:
    """Deja el esquema en ``head`` de forma idempotente y concurrente."""
    config = _build_config()
    with _LOCK:
        with engine.begin() as connection:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _ADVISORY_LOCK_KEY},
                )

            config.attributes["connection"] = connection
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())

            if tables and "alembic_version" not in tables:
                logger.info("Baselineando esquema existente en Alembic 0001_initial")
                _add_legacy_columns(connection, inspector, tables)
                _create_missing_tables(connection, tables)
                command.stamp(config, "head")
            else:
                command.upgrade(config, "head")
