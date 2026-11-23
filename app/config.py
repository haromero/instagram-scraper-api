"""Configuración centralizada leída desde variables de entorno (prefijo IG_).

Los secretos NO tienen valor por defecto: si faltan, la aplicación no arranca
(fail-fast). Así se evita desplegar con el secreto de ejemplo del repositorio.
"""

import ipaddress
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Scopes que puede recibir un cliente normal. ``admin:write`` queda fuera a
# propósito y solo lo añade el cliente de plataforma.
CLIENT_SCOPES: tuple[str, ...] = (
    "profiles:read",
    "posts:read",
    "hashtags:read",
    "search:read",
    "account:read",
    "jobs:read",
    "jobs:write",
    "webhooks:read",
    "webhooks:write",
)
ADMIN_SCOPE = "admin:write"

_WEAK_JWT_SECRETS = {
    "dev-secret-change-me-please-32-bytes-min",
    "change-me-in-production",
    "secret",
    "changeme",
    "supersecret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="IG_",
        extra="ignore",
    )

    app_name: str = "Instagram Scraper API"
    version: str = "1.2.0"
    debug: bool = False
    # Publicar /openapi.json sin habilitar /docs (lo necesita RapidAPI).
    openapi_enabled: bool = False

    database_url: str = "sqlite:///./igscraper.db"

    # --- JWT -----------------------------------------------------------------
    # Obligatorio: >= 32 caracteres y sin valores de ejemplo.
    jwt_secret: str = Field(..., min_length=32)
    # Solo HMAC: evita configurar ``none`` o algoritmos asimétricos por error.
    jwt_algorithm: str = Field(default="HS256", pattern=r"^HS(256|384|512)$")
    jwt_expires_seconds: int = Field(default=900, ge=60, le=86_400)
    jwt_issuer: str = Field(
        default="instagram-scraper-api", min_length=1, max_length=128
    )
    jwt_audience: str = Field(
        default="instagram-scraper-clients", min_length=1, max_length=128
    )
    jwt_leeway_seconds: int = Field(default=10, ge=0, le=120)

    # --- Clientes de la API --------------------------------------------------
    # El secreto se guarda hasheado con PBKDF2-HMAC-SHA256 (ver app/passwords.py).
    client_id: str = Field(..., min_length=3, max_length=128)
    client_secret_hash: str = Field(..., min_length=20)
    client_scopes: str = ",".join(CLIENT_SCOPES)

    # Cliente de plataforma: es el único que recibe admin:write.
    admin_client_id: str | None = Field(default=None, min_length=3, max_length=128)
    admin_client_secret_hash: str | None = Field(default=None, min_length=20)

    # --- Cifrado en reposo ---------------------------------------------------
    # Clave Fernet (44 caracteres urlsafe-base64).
    encryption_key: str = Field(..., min_length=44)

    # --- Límites -------------------------------------------------------------
    rate_limit_per_minute: int = Field(default=600, ge=1)
    auth_rate_limit_per_minute: int = Field(default=5, ge=1)
    jobs_rate_limit_per_minute: int = Field(default=10, ge=1)
    health_ready_rate_limit_per_minute: int = Field(default=30, ge=1)
    requests_limit: int = Field(default=50_000, ge=1)
    concurrent_jobs_limit: int = Field(default=10, ge=1, le=1000)
    max_body_bytes: int = Field(default=1_000_000, ge=1024, le=50_000_000)

    # --- Estado compartido entre workers -------------------------------------
    # ``memory`` mantiene el comportamiento de un solo proceso (default);
    # ``redis`` comparte rate limit, revocación y candados entre workers.
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_timeout_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    # Si Redis cae: True = degradar sin bloquear; False = rechazar (fail-closed).
    rate_limit_fail_open: bool = True
    # La revocación es más sensible que el rate limit: por defecto se rechazan
    # los JWT cuando no se puede comprobar si están revocados.
    revocation_fail_open: bool = False

    # --- Red / despliegue ----------------------------------------------------
    cors_origins: str = ""
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    # Solo estos peers pueden aportar X-Forwarded-For (IPs o CIDR en CSV).
    trusted_proxies: str = ""
    force_https: bool = False
    # Resuelve DNS al validar URLs de salida (bloquea localtest.me y similares).
    validate_outbound_dns: bool = True

    # --- Simulación de scraping ---------------------------------------------
    job_simulation_seconds: float = Field(default=3.0, ge=0.0, le=300.0)
    job_steps: int = Field(default=10, ge=1, le=1000)
    job_lock_ttl_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    job_lock_timeout_seconds: float = Field(default=2.0, ge=0.0, le=30.0)

    # --- Cola y worker ------------------------------------------------------
    # ``thread`` mantiene el arranque local de siempre; ``redis`` publica los
    # jobs en un stream y los consume un proceso worker independiente.
    job_backend: Literal["thread", "redis"] = "thread"
    job_queue_stream: str = Field(default="ig:jobs", min_length=1, max_length=128)
    job_consumer_group: str = Field(default="ig-jobs", min_length=1, max_length=128)
    job_visibility_seconds: int = Field(default=300, ge=30, le=86_400)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_poll_seconds: float = Field(default=1.0, ge=0.05, le=30.0)
    job_claim_batch: int = Field(default=10, ge=1, le=100)

    # --- Entrega de webhooks -------------------------------------------------
    # Se firma el cuerpo con HMAC-SHA256 y se reintenta con backoff exponencial.
    webhook_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)
    webhook_backoff_seconds: float = Field(default=1.0, ge=0.0, le=30.0)
    # Tiempo máximo compartido por todas las entregas de un evento: evita que
    # un receptor lento bloquee el worker o la petición HTTP que cancela un job.
    webhook_delivery_budget_seconds: float = Field(default=10.0, ge=1.0, le=120.0)

    # --- Backend de datos ----------------------------------------------------
    # ``simulated`` es el valor por defecto para que la API arranque sin red.
    scraper_backend: Literal["simulated", "bluesky"] = "simulated"
    # TTL de la caché de perfiles. Con datos reales evita golpear al backend
    # en cada petición; 0 desactiva la caché (siempre refresca).
    profile_cache_ttl_seconds: int = Field(default=3600, ge=0, le=2_592_000)

    # Bluesky / AT Protocol. Las lecturas públicas no necesitan credenciales.
    bluesky_api_base: str = "https://public.api.bsky.app"
    bluesky_appview_base: str = "https://api.bsky.app"
    bluesky_pds_url: str = "https://bsky.social"
    # Cuenta propia (gratuita) para ``searchPosts``. Sin ella, la búsqueda de
    # posts y hashtags devuelve un error explicativo; el resto funciona igual.
    bluesky_handle: str | None = None
    bluesky_app_password: SecretStr | None = None
    bluesky_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    bluesky_max_retries: int = Field(default=3, ge=1, le=10)
    bluesky_backoff_seconds: float = Field(default=1.0, ge=0.0, le=30.0)

    # ------------------------------------------------------------------ validators
    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, value: str) -> str:
        if value.lower() in _WEAK_JWT_SECRETS:
            raise ValueError(
                "IG_JWT_SECRET usa un valor de ejemplo; genera uno aleatorio de 32+ bytes."
            )
        if len(set(value)) < 10:
            raise ValueError("IG_JWT_SECRET es demasiado repetitivo; usa uno aleatorio.")
        return value

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: str) -> str:
        try:
            Fernet(value.encode())
        except Exception as exc:  # pragma: no cover - validación de configuración
            raise ValueError(
                "IG_ENCRYPTION_KEY debe ser una clave Fernet válida "
                "(genera una con: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\")"
            ) from exc
        return value

    @field_validator("client_secret_hash", "admin_client_secret_hash")
    @classmethod
    def _validate_secret_hash(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("pbkdf2_sha256$"):
            raise ValueError(
                "Los secretos de cliente deben guardarse como hash "
                "pbkdf2_sha256$<iteraciones>$<salt>$<hash>."
            )
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, value: str) -> str:
        hosts = [host.strip() for host in value.split(",") if host.strip()]
        if not hosts:
            raise ValueError("IG_ALLOWED_HOSTS no puede estar vacío.")
        if "*" in hosts:
            raise ValueError(
                "IG_ALLOWED_HOSTS no puede ser '*': desactivaría la validación "
                "del cabecero Host."
            )
        return ",".join(hosts)

    @field_validator("client_scopes")
    @classmethod
    def _validate_client_scopes(cls, value: str) -> str:
        scopes = [scope.strip() for scope in value.split(",") if scope.strip()]
        if not scopes:
            raise ValueError("IG_CLIENT_SCOPES no puede estar vacío.")
        if ADMIN_SCOPE in scopes:
            raise ValueError(
                "IG_CLIENT_SCOPES no puede incluir admin:write; ese scope es "
                "exclusivo del cliente de plataforma."
            )
        unknown = sorted(set(scopes) - set(CLIENT_SCOPES))
        if unknown:
            raise ValueError(
                "IG_CLIENT_SCOPES contiene scopes desconocidos: "
                + ", ".join(unknown)
            )
        if len(scopes) != len(set(scopes)):
            raise ValueError("IG_CLIENT_SCOPES contiene scopes duplicados.")
        return ",".join(scopes)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: str) -> str:
        entries = [item.strip() for item in value.split(",") if item.strip()]
        for entry in entries:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "IG_TRUSTED_PROXIES debe contener IPs o redes CIDR válidas."
                ) from exc
        return ",".join(entries)

    @field_validator("bluesky_api_base", "bluesky_appview_base", "bluesky_pds_url")
    @classmethod
    def _validate_bluesky_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(
                "Las URLs de Bluesky deben ser https:// con un host válido."
            )
        if parts.username or parts.password:
            raise ValueError("Las URLs de Bluesky no pueden incluir credenciales.")
        return value.rstrip("/")

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("redis", "rediss") or not parts.hostname:
            raise ValueError(
                "IG_REDIS_URL debe ser redis:// o rediss:// con un host válido."
            )
        return value.rstrip("/")

    @field_validator("bluesky_handle")
    @classmethod
    def _validate_bluesky_handle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        handle = value.strip().lstrip("@").lower()
        if not handle:
            return None
        if "." not in handle or len(handle) > 253:
            raise ValueError(
                "IG_BLUESKY_HANDLE debe ser un handle completo, por ejemplo "
                "'mi-cuenta.bsky.social' (sin @)."
            )
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
        if set(handle) - allowed or handle.startswith("-") or handle.endswith("-"):
            raise ValueError("IG_BLUESKY_HANDLE contiene caracteres no válidos.")
        return handle

    @model_validator(mode="after")
    def _validate_admin_client(self) -> "Settings":
        has_id = bool(self.admin_client_id)
        has_hash = bool(self.admin_client_secret_hash)
        if has_id != has_hash:
            raise ValueError(
                "IG_ADMIN_CLIENT_ID e IG_ADMIN_CLIENT_SECRET_HASH deben "
                "configurarse juntos."
            )
        if has_id and self.admin_client_id == self.client_id:
            raise ValueError(
                "IG_ADMIN_CLIENT_ID debe ser distinto de IG_CLIENT_ID."
            )
        has_handle = bool(self.bluesky_handle)
        has_password = self.bluesky_app_password is not None and bool(
            self.bluesky_app_password.get_secret_value()
        )
        if has_handle != has_password:
            raise ValueError(
                "IG_BLUESKY_HANDLE e IG_BLUESKY_APP_PASSWORD deben configurarse "
                "juntos."
            )
        if self.job_backend == "redis" and self.rate_limit_backend != "redis":
            raise ValueError(
                "IG_JOB_BACKEND=redis necesita IG_RATE_LIMIT_BACKEND=redis: "
                "la cola reutiliza el mismo cliente Redis."
            )
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def client_scopes_list(self) -> list[str]:
        return [scope.strip() for scope in self.client_scopes.split(",") if scope.strip()]

    @property
    def trusted_proxies_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @property
    def admin_scopes_list(self) -> list[str]:
        """El cliente de plataforma es el único que recibe admin:write."""
        scopes = list(self.client_scopes_list)
        if "admin:write" not in scopes:
            scopes.append("admin:write")
        return scopes

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
