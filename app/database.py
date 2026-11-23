"""Motor de base de datos SQLAlchemy y sesión por request."""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import get_settings

settings = get_settings()


def normalize_database_url(url: str) -> str:
    """Render entrega ``postgresql://``; usamos psycopg3 con el driver explícito."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


database_url = normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(database_url, connect_args=connect_args, future=True)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)

Base = declarative_base()


def get_db():
    """Dependencia de FastAPI: abre y cierra una sesión por petición."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
