"""Modelos de persistencia (SQLAlchemy)."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint

from .crypto import EncryptedText
from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False, index=True)
    target = Column(Text, nullable=False, default="{}")
    options = Column(Text, nullable=False, default="{}")
    status = Column(String, nullable=False, default="queued", index=True)
    # Número de intentos de ejecución (lo incrementa el worker al recuperar un
    # job atascado). No se expone en la API para no romper el contrato.
    attempts = Column(Integer, nullable=False, default=0)
    items_scraped = Column(Integer, nullable=False, default=0)
    items_total = Column(Integer, nullable=False, default=0)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    estimated_completion = Column(DateTime(timezone=True), nullable=True)


class Webhook(Base):
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    # La URL puede contener tokens de un solo uso: se cifra en reposo.
    url = Column(EncryptedText, nullable=False)
    events = Column(Text, nullable=False, default="[]")
    secret = Column(EncryptedText, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class WebhookDelivery(Base):
    """Un intento de entrega por (webhook, job, evento).

    La restricción única hace idempotente el despacho: si el worker reintenta
    un job o se cancela mientras se entregaba, no se envía el evento dos veces.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "webhook_id", "job_id", "event", name="uq_webhook_delivery"
        ),
    )

    id = Column(String, primary_key=True)
    webhook_id = Column(String, nullable=False, index=True)
    job_id = Column(String, nullable=False, index=True)
    event = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    response_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    username = Column(String, nullable=False)
    cookies = Column(EncryptedText, nullable=False)
    proxy_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="valid")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)


class Proxy(Base):
    __tablename__ = "proxies"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    url = Column(String, nullable=False)
    username = Column(EncryptedText, nullable=True)
    password = Column(EncryptedText, nullable=True)
    country = Column(String, nullable=True)
    status = Column(String, nullable=False, default="unknown")
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProfileCache(Base):
    __tablename__ = "profile_cache"

    username = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    scraped_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
