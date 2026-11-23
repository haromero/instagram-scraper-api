"""Entorno de Alembic: usa la misma configuración y metadatos que la app.

La aplicación pasa la conexión (o el motor) mediante ``config.attributes`` para
no duplicar la URL de la base de datos ni abrir una segunda conexión.
"""

from __future__ import annotations

from sqlalchemy import create_engine

from alembic import context

from app import models  # noqa: F401  (registra las tablas en el metadata)
from app.config import get_settings
from app.database import normalize_database_url

config = context.config
target_metadata = models.Base.metadata


def _url() -> str:
    return normalize_database_url(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url") or _url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # La app ya tiene una transacción abierta (y, en Postgres, un candado
        # advisory para serializear migraciones entre réplicas).
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        context.run_migrations()
        return

    engine = config.attributes.get("engine")
    if engine is None:
        engine = create_engine(
            config.get_main_option("sqlalchemy.url") or _url(), future=True
        )
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
