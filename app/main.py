"""Punto de entrada de la aplicación FastAPI."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import locks, models, revocation  # noqa: F401  (models registra tablas)
from .config import get_settings
from .database import engine
from .db_migrations import upgrade_database
from .errors import register_exception_handlers
from .middleware import (
    MaxBodySizeMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from .ratelimit import build_rate_limiter
from .redis_backend import build_redis_client, set_client
from .routers import (
    account,
    admin,
    auth,
    hashtags,
    health,
    jobs,
    posts,
    profiles,
    search,
    webhooks,
)
from .services import scraper
from .services.jobs import (
    build_job_queue,
    configure as configure_jobs,
    shutdown as shutdown_jobs,
)

settings = get_settings()

# Un único cliente Redis (o ``None`` con el backend memory) compartido por el
# rate limit, la revocación de tokens y el candado de cuota de jobs.
redis_client = build_redis_client(settings)
set_client(redis_client)
limiter = build_rate_limiter(settings, client=redis_client)
revocation.configure(revocation.build_revocation_store(settings, client=redis_client))
locks.configure(locks.build_quota_lock(settings, client=redis_client))
configure_jobs(build_job_queue(settings, client=redis_client))


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Cierra el pool de jobs y el cliente HTTP del backend para no dejar
    # hilos ni conexiones vivas al apagar.
    shutdown_jobs()
    scraper.reset_backend()
    if redis_client is not None:
        redis_client.close()

# Alembic crea el esquema o baselanea una BD SQLite anterior.
upgrade_database(engine)

_docs_enabled = settings.debug
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "API REST para extracción de datos públicos de Instagram. "
        "Implementación de referencia con datos simulados y jobs asíncronos."
    ),
    contact={"name": "API Support", "email": "api@igscraper.example.com"},
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if settings.openapi_enabled else None,
)

# El último middleware añadido es el más externo. El orden buscado
# (de fuera hacia dentro) es:
#   CORS -> SecurityHeaders -> RequestContext -> TrustedHost -> HTTPS
#   -> RateLimit -> MaxBodySize -> rutas
# Así las cabeceras de seguridad y el X-Request-ID llegan también a los
# rechazos de TrustedHost, del rate limit y del límite de body. TrustedHost
# valida el Host antes de que HTTPSRedirect construya un Location, y el rate
# limit cuenta también las peticiones que luego se rechazan por tamaño.
app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(RateLimitMiddleware, limiter=limiter)
if settings.force_https:
    app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.allowed_hosts_list,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

register_exception_handlers(app)

api = APIRouter(prefix="/api/v1")
api.include_router(auth.router)
api.include_router(jobs.router)
api.include_router(profiles.router)
api.include_router(posts.router)
api.include_router(hashtags.router)
api.include_router(search.router)
api.include_router(webhooks.router)
api.include_router(account.router)
api.include_router(health.router)
api.include_router(admin.router)
app.include_router(api)


@app.get("/", include_in_schema=False)
def root() -> dict:
    payload = {"name": settings.app_name, "version": settings.version}
    if _docs_enabled:
        payload["docs"] = "/docs"
        payload["openapi"] = "/openapi.json"
    return payload
