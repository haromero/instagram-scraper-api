# Instagram Scraper API (FastAPI)

Implementación de referencia de la API REST diseñada para un scraper de Instagram.
Incluye **30 endpoints**, autenticación JWT, jobs asíncronos, paginación por cursor,
validación con Pydantic, manejo uniforme de errores y un hardening basado en el
**OWASP API Security Top 10 (2023)**.

> ℹ️ **Dos backends de datos.** Por defecto la API usa datos simulados
> deterministas (`IG_SCRAPER_BACKEND=simulated`) para arrancar sin red. Con
> `IG_SCRAPER_BACKEND=bluesky` sirve **datos reales** de la API pública de
> Bluesky/AT Protocol: gratis, oficial y sin cuentas que puedan ser baneadas.
> El contrato de los 30 endpoints, los jobs, la caché y los webhooks no cambian.

---

## Requisitos

- Python **3.10+** (probado con 3.14)
- pip

## Instalación

```powershell
cd instagram_scraper_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

En macOS/Linux:

```bash
cd instagram_scraper_api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Genera los secretos obligatorios

La aplicación **no arranca** si faltan `IG_JWT_SECRET`, `IG_ENCRYPTION_KEY`,
`IG_CLIENT_ID` o `IG_CLIENT_SECRET_HASH` (fail-fast). No hay valores por defecto.

```powershell
# Secreto JWT (48 bytes urlsafe)
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Clave Fernet para cifrar secretos en reposo
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Hash PBKDF2 del secreto de cliente (te pide el secreto por consola)
python scripts/hash_secret.py
```

Copia cada valor en `.env`. El secreto en claro **nunca** se guarda: solo su hash
`pbkdf2_sha256$...`.

### 2. Cliente normal y cliente de plataforma

| Variable | Uso |
|----------|-----|
| `IG_CLIENT_ID` + `IG_CLIENT_SECRET_HASH` | Cliente normal. Scopes de lectura/escritura, **sin** `admin:write`. |
| `IG_ADMIN_CLIENT_ID` + `IG_ADMIN_CLIENT_SECRET_HASH` | Cliente de plataforma. Es el único que recibe `admin:write`. |

## Ejecutar en local

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

- API: <http://127.0.0.1:8000>
- Swagger UI: <http://127.0.0.1:8000/docs> (solo si `IG_DEBUG=true`)
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json> (solo si `IG_DEBUG=true`)

La base de datos SQLite (`igscraper.db`) se crea automáticamente al arrancar.
Si ya existía, `app/migrations.py` añade las columnas nuevas (`owner` en
`sessions` y `proxies`).

### Migrar secretos existentes

Si vienes de la versión anterior y tu base tiene cookies, usuarios o
contraseñas de proxy, URLs o secretos de webhook en texto plano:

```powershell
python scripts/migrate_secrets.py
```

El script recorre las filas, fuerza el `UPDATE` con `flag_modified` y las vuelve
a escribir a través de `EncryptedText`, que cifra con Fernet. Es idempotente:
volver a ejecutarlo no cifra dos veces el mismo valor.

---

## Backend de datos

La fachada `app/services/scraper.py` delega en un backend intercambiable
(`app/services/scraper_base.py`). Los routers y `jobs.py` no cambian.

| `IG_SCRAPER_BACKEND` | Fuente | Red | Uso |
|---|---|---|---|
| `simulated` (default) | Datos deterministas | No | Desarrollo, tests, demo |
| `bluesky` | API pública de Bluesky | Sí | Datos reales sin coste |

### Bluesky

```powershell
$env:IG_SCRAPER_BACKEND="bluesky"
.\.venv\Scripts\uvicorn.exe app.main:app --reload
```

No hace falta cuenta ni token para perfiles, posts, seguidores, follows, hilos,
likes, búsqueda de actores y la primera página de `searchPosts`. Si quieres que
la búsqueda de posts pagine y use tu propio PDS, crea una **app password**
gratuita en Bluesky (`Settings > App Passwords`) y define:

```dotenv
IG_BLUESKY_HANDLE=mi-cuenta.bsky.social
IG_BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

Mapeo al contrato actual:

| Endpoint | API de Bluesky |
|---|---|
| `GET /profiles/{username}` | `app.bsky.actor.getProfile` |
| `GET /profiles/{u}/posts` | `app.bsky.feed.getAuthorFeed` |
| `GET /profiles/{u}/followers` | `app.bsky.graph.getFollowers` |
| `GET /profiles/{u}/following` | `app.bsky.graph.getFollows` |
| `GET /posts/{shortcode}` | `app.bsky.feed.getPosts` |
| `GET /posts/{s}/comments` | `app.bsky.feed.getPostThread` |
| `GET /posts/{s}/likes` | `app.bsky.feed.getLikes` |
| `GET /hashtags/{tag}` y `/posts` | `app.bsky.feed.searchPosts` |
| `GET /search` | `app.bsky.actor.searchActors` + `searchPosts` |

Notas de contrato con el backend real:

- `username` es el handle (`alice.bsky.social`) o el DID; `user_id` es el DID.
- `shortcode` es el compuesto `did|rkey` (o un AT URI completo). Si lo usas en
  una URL, codifica el `|` como `%7C`.
- `taken_at` viene de `record.createdAt`; `id` es el AT URI del post.
- `stories` y `highlights` devuelven `[]`: Bluesky no tiene esos conceptos.
- `type` puede ser `text`, `image`, `carousel` o `video`.
- `media_count` de un hashtag es el número de posts recuperados en esa consulta,
  no un contador global (Bluesky no lo expone).
- Sin credenciales, `searchPosts` devuelve como máximo 100 resultados porque el
  AppView público bloquea el cursor; con app password se pagina completo.
- La caché de perfiles respeta `IG_PROFILE_CACHE_TTL_SECONDS` (1 hora por
  defecto; `0` la desactiva).

Comprobación contra la API real (sin mocks):

```powershell
.\.venv\Scripts\python.exe scripts\check_bluesky.py
.\.venv\Scripts\python.exe scripts\check_bluesky.py --actor bsky.app
```

---

## Autenticación

```powershell
$body = @{ client_id = "cli_abc123"; client_secret = "<tu-secreto>" } | ConvertTo-Json
$token = (Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/auth/token `
  -ContentType "application/json" -Body $body).access_token

$headers = @{ Authorization = "Bearer $token" }
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"cli_abc123","client_secret":"<tu-secreto>"}'
```

Todas las rutas (salvo `/auth/token` y `/health*`) requieren
`Authorization: Bearer <token>`. Un 401 incluye `WWW-Authenticate: Bearer`.

### Scopes

| Scope | Endpoints |
|-------|-----------|
| `profiles:read` | `GET /profiles/*` |
| `posts:read` | `GET /posts/*` |
| `hashtags:read` | `GET /hashtags/*` |
| `search:read` | `GET /search` |
| `account:read` | `GET /account/*` |
| `jobs:read` / `jobs:write` | `GET /jobs*` / `POST` y `DELETE /jobs*` |
| `webhooks:read` / `webhooks:write` | `GET /webhooks` / `POST` y `DELETE /webhooks*` |
| `admin:write` | `/admin/*` (solo cliente de plataforma) |

`force_refresh=true` en perfiles y hashtags exige además `jobs:write`.

---

## Ejemplos rápidos

### 1. Perfil

```bash
curl http://127.0.0.1:8000/api/v1/profiles/nasa -H "Authorization: Bearer $TOKEN"
```

### 2. Crear un job (202 Accepted)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "profile_posts",
    "target": {"username": "nasa"},
    "options": {"limit": 50}
  }'
```

### 3. Consultar estado del job

```bash
curl http://127.0.0.1:8000/api/v1/jobs/<JOB_ID> -H "Authorization: Bearer $TOKEN"
```

### 4. Obtener resultado (solo si `status == completed`)

```bash
curl http://127.0.0.1:8000/api/v1/jobs/<JOB_ID>/result -H "Authorization: Bearer $TOKEN"
```

### 5. Refresco forzado (devuelve 202 con un job)

```bash
curl "http://127.0.0.1:8000/api/v1/profiles/nasa?force_refresh=true" \
  -H "Authorization: Bearer $TOKEN"
```

### 6. Búsqueda

```bash
curl "http://127.0.0.1:8000/api/v1/search?q=nasa&type=all&limit=10" \
  -H "Authorization: Bearer $TOKEN"
```

### 7. Webhook

```bash
curl -X POST http://127.0.0.1:8000/api/v1/webhooks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://mi-app.com/hooks/ig","events":["job.completed","job.failed"]}'
```

Si no envías `secret`, se genera uno y se devuelve **solo en esta respuesta**
(queda cifrado en la BD y no aparece en `GET /webhooks`). Guardalo: es la clave
con la que se firman las entregas.

Cuando un job termina se envía un `POST` JSON al webhook con:

| Cabecero | Contenido |
|----------|-----------|
| `X-Signature` | `sha256=<hex>` de HMAC-SHA256 sobre `"<timestamp>.<cuerpo crudo>"` |
| `X-Webhook-Timestamp` | Unix UTC usado en la firma (rechaza firmas con > 5 min de antigüedad) |
| `X-Webhook-Event` | `job.completed`, `job.failed` o `job.cancelled` |
| `X-Webhook-Delivery` | ID estable de la entrega (idempotencia) |

El cuerpo es JSON canónico (`sort_keys`, sin espacios): `id`, `event`,
`created_at` y `data` con `job_id`, `type`, `status`, `target`, `progress`,
`result_url`, `error` y fechas. El receptor puede verificarlo así:

```python
from app.services.webhooks import verify_signature

valido = verify_signature(
    secreto,
    request.headers["X-Webhook-Timestamp"],
    await request.body(),  # bytes crudos, sin re-serializar el JSON
    request.headers["X-Signature"],
)
```

Hay una fila en `webhook_deliveries` por `(webhook, job, evento)`: un reintento
del worker o una cancelación concurrente no duplican el envío. Los fallos se
reintentan con backoff exponencial y se registran con `attempts`,
`response_code` y `error`. Un webhook caído nunca cambia el estado del job.

### 8. Health (sin token)

```bash
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/health/ready
```

---

## Endpoints

Todas las rutas llevan el prefijo `/api/v1`.

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/auth/token` | Obtener access token |
| POST | `/jobs` | Encolar un job |
| GET | `/jobs` | Listar jobs |
| GET | `/jobs/{job_id}` | Estado de un job |
| GET | `/jobs/{job_id}/result` | Resultado paginado |
| DELETE | `/jobs/{job_id}` | Cancelar un job |
| GET | `/profiles/{username}` | Perfil (caché) |
| GET | `/profiles/{username}/posts` | Posts del perfil |
| GET | `/profiles/{username}/followers` | Seguidores |
| GET | `/profiles/{username}/following` | Cuentas seguidas |
| GET | `/profiles/{username}/stories` | Stories activas |
| GET | `/profiles/{username}/highlights` | Highlights |
| GET | `/posts/{shortcode}` | Detalle de un post |
| GET | `/posts/{shortcode}/comments` | Comentarios |
| GET | `/posts/{shortcode}/likes` | Likes |
| GET | `/hashtags/{hashtag}` | Metadatos del hashtag |
| GET | `/hashtags/{hashtag}/posts` | Posts del hashtag |
| GET | `/search` | Búsqueda |
| POST | `/webhooks` | Registrar webhook |
| GET | `/webhooks` | Listar webhooks |
| DELETE | `/webhooks/{webhook_id}` | Eliminar webhook |
| GET | `/account/usage` | Consumo |
| GET | `/account/limits` | Límites |
| GET | `/health` | Liveness |
| GET | `/health/ready` | Readiness |
| POST | `/admin/sessions` | Cargar sesión |
| GET | `/admin/sessions` | Listar sesiones |
| DELETE | `/admin/sessions/{session_id}` | Revocar sesión |
| POST | `/admin/proxies` | Añadir proxy |
| GET | `/admin/proxies` | Listar proxies |

---

## Seguridad aplicada (OWASP API Top 10)

| ID | Hallazgo | Fix implementado |
|----|----------|------------------|
| C1 | Secretos por defecto en el repo | `Settings` exige `IG_JWT_SECRET` y `IG_CLIENT_SECRET_HASH`; validadores rechazan valores débiles. Secreto de cliente con PBKDF2-HMAC-SHA256 (600k iteraciones) y comparación en tiempo constante. |
| C2 | Todos los tokens recibían `admin:write` | Scopes por cliente; `admin:write` solo para `IG_ADMIN_CLIENT_ID`. |
| M1 | Scopes declarados pero no exigidos | `require_scope` en todos los endpoints de lectura/escritura, incluidos jobs y webhooks. |
| A1 | Un hilo por job sin límite | `ThreadPoolExecutor` con `max_workers=IG_CONCURRENT_JOBS_LIMIT`, cuota de jobs concurrentes y cuota mensual en `enqueue`. |
| A2 | Rate limit en memoria sin purga | Ventana deslizante con purga y evicción LRU real, clave por `sub` + `tenant` del token (o IP del socket), cubos separados por tipo (`general`/`auth`/`jobs`/`ready`), `Retry-After` y `X-RateLimit-*`. Backend `memory` por defecto o `redis` con ZSET atómica (`ZADD`/`ZREMRANGEBYSCORE` en Lua) compartido por todos los workers. `X-Forwarded-For` solo se lee si el peer está en `IG_TRUSTED_PROXIES`. |
| A3 | Cookies, contraseñas y secretos en claro | `EncryptedText` (Fernet) en `Session.cookies`, `Proxy.username`, `Proxy.password`, `Webhook.url` y `Webhook.secret`; userinfo de proxies separado y nunca devuelto. La migración fuerza el `UPDATE` con `flag_modified`. |
| A4 | SSRF en URLs de webhook/proxy | `assert_safe_outbound_url` / `assert_safe_proxy_url`: solo http/https, bloqueo de userinfo, localhost, rangos privados, loopback, link-local, reservados y IPv4 mapeada en IPv6 (`::ffff:127.0.0.1`). Resuelve DNS al validar (`IG_VALIDATE_OUTBOUND_DNS`) para cortar bypasses como `localtest.me`. |
| A5 | `str(exc)` devuelto al cliente | `logger.exception` en servidor; el cliente recibe un mensaje genérico `JOB_FAILED` sin rutas, SQL ni detalles internos. |
| M2 | CORS `*` y docs públicos | CORS desde `IG_CORS_ORIGINS`; `/docs`, `/redoc` y `/openapi.json` solo con `IG_DEBUG=true`. |
| M3 | Sin cabeceras de seguridad | `TrustedHostMiddleware`, `HTTPSRedirectMiddleware` opcional, HSTS, `nosniff`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` y `Cache-Control: no-store`. El orden de middlewares garantiza cabeceras y `X-Request-ID` también en rechazos de Host, rate limit y body. |
| M4 | Sin aislamiento multi-tenant | `owner` en sesiones y proxies, filtros por tenant y `tenant` en el JWT. |
| M5 | JWT sin `aud`, `iss` ni claims obligatorios | `iss`, `aud`, `iat`, `nbf`, `exp` y `jti`; `decode_token` exige todos, verifica emisor/audiencia con leeway y valida el tipo/tamaño de `sub`, `jti`, `scopes` y `tenant`. Algoritmo restringido a HS256/384/512. Vida del token: 15 min. |
| M6 | Sin límite de body ni `max_length` | `MaxBodySizeMiddleware` ASGI puro (1 MB → 413, también con `Transfer-Encoding: chunked`), cursores acotados a 512 caracteres y `max_length` en cookies, credenciales, usuarios, eventos, secretos y hashtags. |
| B1 | 401 sin `WWW-Authenticate` | `APIError` acepta `headers`; los 401 devuelven `WWW-Authenticate: Bearer`. |
| B2 | Sin auditoría | `app/audit.py` registra login, alta/baja de sesión y proxy, creación/cancelación de job y webhooks. |
| B3 | `X-Request-ID` de 48 bits | Se acepta un ID de cliente validado o se genera un UUID4 completo. |
| B4 | Sin política de versionado | Prefijo `/api/v1`; cambios incompatibles van a `/api/v2`. |

### Estado compartido entre workers

Por defecto (`IG_RATE_LIMIT_BACKEND=memory`) el rate limit, la revocación y el
candado de cuota viven en memoria del proceso: ideal para desarrollo y para una
sola réplica, sin dependencias adicionales.

Para despliegues con varios workers, configura Redis y todos comparten el mismo
estado sin cambiar el contrato HTTP:

```powershell
$env:IG_RATE_LIMIT_BACKEND="redis"
$env:IG_REDIS_URL="redis://localhost:6379/0"
$env:IG_RATE_LIMIT_FAIL_OPEN="true"
$env:IG_REVOCATION_FAIL_OPEN="false"
$env:IG_TRUSTED_PROXIES="10.0.0.0/8"
```

| Componente | Implementación Redis |
|------------|----------------------|
| Rate limit | Script Lua con ZSET en ventana deslizante, `PEXPIRE` y hash tag por cliente; mismo contrato `Retry-After`/`X-RateLimit-*` que el backend memoria. |
| Revocación de tokens | `SET` con TTL hasta que expira el JWT; la ven todos los workers. Si Redis cae, la revocación queda pendiente en local y se reintenta; por defecto se rechaza el JWT si no se puede comprobar (`IG_REVOCATION_FAIL_OPEN=false`). |
| Cuota de jobs | Candado distribuido `SET NX PX`, renovación con heartbeat, liberación verificada por Lua y reintento breve; la comprobación y el `INSERT` quedan serializados por `owner`. |

Política de degradación: con `IG_RATE_LIMIT_FAIL_OPEN=true` (default) un corte
de Redis deja pasar el tráfico general y marca la respuesta con
`X-RateLimit-Degraded: 1`, pero **sigue rechazando** `/auth/token` (fail-closed)
para no permitir fuerza bruta sin control. Con `false`, todo se rechaza.
La revocación de JWT tiene política propia (`IG_REVOCATION_FAIL_OPEN`, default
`false`) para no aceptar tokens durante un corte de Redis, y las revocaciones
hechas mientras Redis está caído se reintentan al recuperarse.
`/health/ready` devuelve `503` si Redis está configurado y no responde, si la
cola de jobs está apagada o si la base de datos falla, y sus `checks` se
calculan del estado real (pool y proxies). Tiene su propio cubo de 30 req/min
para no ser un vector de DoS.

Opcionalmente, para arrancar Redis localmente:

```powershell
docker compose --env-file .env.compose up -d redis
```

---

## Despliegue (objetivo: RapidAPI o Render)

Puedes validar los artefactos sin Docker con:

```powershell
.\.venv\Scripts\python.exe scripts/check_deployment.py
```

### Local con Docker Compose

El `docker-compose.yml` levanta la API, un **worker real** (`python -m
app.worker`) y Redis. Copia `.env.example` a `.env`, rellena los secretos y
ejecuta:

```powershell
docker compose --env-file .env.compose up --build
```

`.env.compose` está vacío a propósito: evita que Compose interpole el `.env`
real (los hashes PBKDF2 contienen `$`). Los secretos se inyectan igualmente
desde `.env` gracias a `env_file: format: raw` en `docker-compose.yml`.

La API queda en `http://127.0.0.1:8000/api/v1` y el healthcheck en
`http://127.0.0.1:8000/api/v1/health`. Redis solo se publica en `127.0.0.1`.
Si no quieres Redis, usa `IG_RATE_LIMIT_BACKEND=memory`,
`IG_JOB_BACKEND=thread` y levanta solo el servicio `api`.

Alembic aplica el esquema en el arranque de la API y del worker, así que no
hay que ejecutar migraciones a mano. Para crear una revisión nueva:

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "cambio"
```

### Render

`render.yaml` es un Blueprint que crea el servicio web (desde el `Dockerfile`),
una base de datos PostgreSQL y una instancia de Key Value (Redis). El comando
`python -m app.launch` arranca, dentro del mismo plan gratuito, Gunicorn y el
worker de Redis Streams como procesos separados: si uno muere, se detiene el
otro. Para escalar el consumo, añade un servicio `type: worker` con la misma
imagen y `dockerCommand: python -m app.worker`.

1. Sube el proyecto a GitHub/GitLab.
2. En Render: **New → Blueprint** (no *Web Service*) y selecciona el
   repositorio, rama `main` y `render.yaml`. El Blueprint crea
   `instagram-scraper-api`, `ig-postgres` e `ig-redis` y cablea sus URLs.
   Si ya creaste un *Web Service* normal, bórralo antes: no crea la BD ni
   Redis y deja `dockerCommand`/`healthCheckPath` vacíos.
3. Genera un set nuevo de secretos con:

```powershell
.\.venv\Scripts\python.exe scripts/generate_secrets.py
# o, para no imprimirlos en pantalla:
.\.venv\Scripts\python.exe scripts/generate_secrets.py --out "$env:LOCALAPPDATA\Temp\opencode\render-secrets.txt"
```

4. Pega en el formulario del Blueprint los valores de `IG_ENCRYPTION_KEY`,
   `IG_CLIENT_SECRET_HASH` e `IG_ADMIN_CLIENT_SECRET_HASH`. `IG_JWT_SECRET` lo
   genera Render (`generateValue: true`); si lo rotas manualmente, actualízalo
   también en el servicio. Guarda `CLIENT_SECRET` y `ADMIN_SECRET` para el smoke.
5. Verifica `https://<servicio>.onrender.com/api/v1/health`.
6. `IG_OPENAPI_ENABLED=true` publica `/openapi.json` sin habilitar `/docs`
   (`IG_DEBUG=false`); así se puede importar el contrato en RapidAPI.
7. El plan `free` duerme tras inactividad. Para producción, sube el plan y
   ajusta `WEB_CONCURRENCY`; con Redis configurado, varios workers comparten
   rate limit, revocación, candados y la cola de jobs.
8. La Key Value gratuita no persiste datos. Si Redis se reinicia, el worker
   republica al arrancar los jobs que siguen en `queued` en PostgreSQL; la
   ejecución es idempotente. Para no perder mensajes, usa un plan con
   persistencia.

Si vas a gestionar Render desde una API key (por ejemplo con el MCP de
Render), crea la key con **All** en el workspace, o al menos: Workspaces y
Services en lectura, Environment variables y Deploys en lectura/escritura,
Monitoring en lectura, y Postgres/Key Value en lectura/escritura. Una key sin
esos permisos puede leer el servicio pero no crear la BD ni Redis (`forbidden`).

`IG_JOB_VISIBILITY_SECONDS` controla cuándo se recupera un mensaje cuyo worker
murió; `IG_JOB_MAX_ATTEMPTS` cuántos intentos se permiten antes de moverlo al
stream `<cola>:dead`. `IG_FORCE_HTTPS=false` es intencionado: Render termina
TLS y forzar HTTPS dentro del contenedor rompería el healthcheck interno.

### RapidAPI

1. Despliega primero en Render (o cualquier host público HTTPS).
2. En RapidAPI crea el proveedor y usa como base URL
   `https://<servicio>.onrender.com/api/v1`.
3. Importa el spec desde `/openapi.json`.
4. RapidAPI valida `X-RapidAPI-Key` en su gateway; esta API **no** lo valida
   por sí sola. Si quieres autenticación propia, llama a `POST /auth/token` y
   envía `Authorization: Bearer <token>` en las peticiones proxyadas.
5. Si el proxy de RapidAPI cambia el cabecero `Host`, añade su dominio a
   `IG_ALLOWED_HOSTS` y configura `IG_TRUSTED_PROXIES` con sus IPs/CIDR.

### Limitaciones conocidas

- Con el backend `memory` (default) cada worker tiene sus propios contadores;
  usa `IG_RATE_LIMIT_BACKEND=redis` si despliegas más de una réplica.
- `ProfileCache` es global (los datos públicos de un perfil se comparten).
- La entrega de webhooks es *at-least-once* con reintentos inmediatos (hasta
  `IG_WEBHOOK_MAX_ATTEMPTS`) y un presupuesto total de
  `IG_WEBHOOK_DELIVERY_BUDGET_SECONDS` para no bloquear el worker ni la
  petición que cancela un job. Si el receptor sigue caído, el registro queda
  `failed` en `webhook_deliveries`; no hay una cola de reenvío diferido.
- SQLite y `IG_JOB_BACKEND=thread` son válidos para desarrollo; en producción
  usa PostgreSQL y `IG_JOB_BACKEND=redis`. La cola usa Redis Streams con
  consumer group, visibilidad, reintentos y dead letter; no necesita Celery.
- El esquema se gestiona con Alembic. No vuelvas a usar
  `Base.metadata.create_all` en producción: las bases antiguas se baselanean
  automáticamente la primera vez.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

La suite (`tests/test_security.py`, `tests/test_hardening.py`,
`tests/test_bluesky_backend.py`, `tests/test_ratelimit.py`,
`tests/test_revocation.py`, `tests/test_locks.py`,
`tests/test_feedback_loop.py`, `tests/test_deployment.py`,
`tests/test_worker_migrations.py` y `tests/test_webhooks.py`) cubre:

- login correcto/incorrecto y `WWW-Authenticate`
- 403 en `/admin` con token normal y 200 con el de plataforma
- 403 al crear jobs sin `jobs:write`
- SSRF en webhooks (`127.0.0.1`, `169.254.169.254`, `localtest.me` y IPv4 mapeada en IPv6)
- body > 1 MB → 413, también con `Transfer-Encoding: chunked`
- JWT sin `exp`, con audiencia incorrecta, revocado o con claims de tipo incorrecto → 401
- configuración fail-fast: `admin:write` en scopes normales, algoritmo JWT inseguro, hosts vacíos o `*`, admin duplicado y límites de simulación
- cuota concurrente de jobs, cuota mensual y aislamiento de jobs entre tenants
- error de job sin filtrar detalles internos
- cabeceras de seguridad y `X-Request-ID` en respuestas de error
- cifrado en reposo de cookies, usuario/contraseña de proxy y URL de webhook
- migración de valores heredados en texto plano
- propiedad del proxy al crear una sesión
- docs deshabilitadas con `IG_DEBUG=false`, cursores y `max_length`
- mapeo de Bluesky a los esquemas Pydantic (perfil, posts, comentarios, likes,
  followers, hashtags y búsqueda) con `httpx.MockTransport`, sin red
- reintentos ante 429, mapeo de 400/404/403 y errores de red a `APIError`
- sesión opcional en el PDS y degradación de la búsqueda sin credenciales
- validación fail-fast de las URLs y credenciales de Bluesky
- selección y reset del backend en la fachada
- TTL de la caché de perfiles
- rate limit compartido: contador único entre dos workers simulados, 429 con
  `Retry-After`/`X-RateLimit-*`, degradación fail-open/fail-closed y selección
  de backend; cubos separados por tipo aunque los límites coincidan, hash tag
  común por cliente (incluido `tenant`), sin duplicación de ráfaga en el borde
  de ventana y degradación con el cliente Redis real ante un puerto cerrado
- `X-Forwarded-For` ignorado si el peer no es un proxy de confianza
- evicción LRU real del store memoria al superar el máximo interno de claves
- revocación compartida entre workers, TTL, persistencia de revocaciones
  durante un corte de Redis y política fail-closed por defecto
- candado distribuido: exclusión mutua por owner, liberación solo por el dueño,
  renovación del TTL en secciones largas y cuota que no se multiplica entre workers
- readiness con estado real del pool y los proxies, 503 si la cola o Redis caen
- regresiones del feedback loop: `verifiedStatus` inválido, anclaje del TTL de
  caché, renovación de sesión Bluesky en el último reintento y cierre del
  cliente HTTP al cambiar de backend
- artefactos de Docker/Render y publicación de `/openapi.json` sin `/docs`
- Alembic: esquema completo desde cero y baselining de una SQLite anterior
- cola Redis Streams: publicar/reclamar/confirmar, reintentos con intentos,
  dead letter y recuperación de jobs `running` atascados
- worker: completar y confirmar, reintentar, agotar intentos y recuperar jobs
  huérfanos; readiness informa del backend y del backlog real
- webhooks: firma HMAC y verificación en tiempo constante, rechazo de firmas
  manipuladas o antiguas, secreto generado y cifrado, entregas por suscripción,
  idempotencia, reintentos, dead letter de entrega, bloqueo SSRF en el envío y
  despacho en los tres estados terminales del job

**169 tests** en total.

### Smoke y verificación en vivo

Con el servidor en marcha y las credenciales en variables de entorno:

```powershell
$env:IG_SMOKE_CLIENT_ID="cli_..."
$env:IG_SMOKE_CLIENT_SECRET="..."
$env:IG_SMOKE_ADMIN_ID="cli_platform"
$env:IG_SMOKE_ADMIN_SECRET="..."

# Ejercita los 30 endpoints (32 comprobaciones)
.\.venv\Scripts\python.exe scripts\smoke_endpoints.py

# Verifica en vivo scope, SSRF, cabeceras, body, cifrado, docs y rate limit
.\.venv\Scripts\python.exe scripts\live_security_checks.py

# Comprueba el adaptador de Bluesky contra la API real
.\.venv\Scripts\python.exe scripts\check_bluesky.py
```

El chequeo de rate limit de autenticación va al final porque consume el bucket
de la IP durante 60 s.

---

## Estructura de carpetas

```
instagram_scraper_api/
├── app/
│   ├── __init__.py
│   ├── main.py            # App FastAPI, middlewares y montaje de routers
│   ├── config.py          # Settings obligatorios y validadores (fail-fast)
│   ├── database.py        # Engine SQLAlchemy + dependencia get_db
│   ├── models.py          # Tablas; columnas sensibles con EncryptedText
│   ├── schemas.py         # Modelos Pydantic + validación SSRF y max_length
│   ├── security.py        # JWT con iss/aud/claims y revocación por jti
│   ├── revocation.py      # Store de jti revocados: memoria o Redis
│   ├── ratelimit.py       # RateLimiter: ventana fija en memoria o Redis (Lua)
│   ├── locks.py           # Candado de cuota: local o distribuido (SET NX)
│   ├── redis_backend.py   # Cliente Redis único y registro para health
│   ├── dependencies.py    # get_current_client, require_scope, tenant
│   ├── passwords.py       # PBKDF2-HMAC-SHA256 para secretos de cliente
│   ├── crypto.py          # Fernet + TypeDecorator EncryptedText
│   ├── validators.py      # Validación anti-SSRF y separación de userinfo
│   ├── audit.py           # Log de auditoría en JSON
│   ├── errors.py          # APIError con headers + envelope uniforme
│   ├── context.py         # request_id vía ContextVar
│   ├── middleware.py      # request-id, rate limit, body size, security headers
│   ├── migrations.py      # Columnas nuevas en SQLite heredadas (legacy)
│   ├── db_migrations.py   # Alembic en arranque + baselining de BD antiguas
│   ├── queue.py           # Cola Redis Streams (claim/ack/retry/dead letter)
│   ├── worker.py          # Proceso consumidor de jobs fuera del HTTP
│   ├── launch.py          # Arranca gunicorn + worker en un solo plan
│   ├── pagination.py      # Cursor opaco (base64 offset) y helper paginate
│   ├── routers/
│   │   ├── auth.py        # POST /auth/token
│   │   ├── jobs.py        # CRUD de jobs + resultado
│   │   ├── profiles.py    # Perfiles, posts, followers, stories, highlights
│   │   ├── posts.py       # Posts, comentarios, likes
│   │   ├── hashtags.py    # Hashtags y sus posts
│   │   ├── search.py      # Búsqueda
│   │   ├── webhooks.py    # Webhooks
│   │   ├── account.py     # Consumo y límites
│   │   ├── health.py      # Liveness / readiness
│   │   └── admin.py       # Sesiones y proxies
│   └── services/
│       ├── scraper_base.py      # Protocol común de backends
│       ├── scraper_simulated.py # Backend determinista (default)
│       ├── scraper_bluesky.py   # Backend real sobre AT Protocol
│       ├── scraper.py           # Fachada: elige backend y mantiene firmas
│       ├── jobs.py              # Cola/pool, cuotas y ejecución de jobs
│       └── webhooks.py          # Firma HMAC, reintentos y registro de entregas
├── alembic/
│   ├── env.py                # Metadatos y conexión compartida con la app
│   └── versions/
│       ├── 0001_initial.py      # Esquema completo
│       └── 0002_webhook_deliveries.py # Idempotencia de entregas
├── scripts/
│   ├── hash_secret.py         # Genera el hash PBKDF2 de un secreto
│   ├── migrate_secrets.py     # Cifra secretos heredados en texto plano
│   ├── smoke_endpoints.py     # Ejercita los 30 endpoints contra el servidor
│   ├── live_security_checks.py # Verificación de seguridad en vivo
│   ├── check_bluesky.py       # Prueba el adaptador contra la API real
│   └── check_deployment.py    # Valida Dockerfile/Compose/Render sin Docker
├── tests/
│   ├── conftest.py        # Entorno aislado + TestClient
│   ├── test_security.py   # Regresión de los hallazgos OWASP
│   ├── test_hardening.py  # Feedback loop: configuración, JWT, jobs, cifrado y migración
│   ├── test_bluesky_backend.py # Mapeo y errores del backend real (sin red)
│   ├── test_ratelimit.py  # Limiter memory/Redis y contrato 429 del middleware
│   ├── test_revocation.py # Stores de revocación memory/Redis
│   ├── test_locks.py      # Candados, cuota atómica y readiness de Redis
│   ├── test_deployment.py # Artefactos Docker/Render y OpenAPI
│   ├── test_worker_migrations.py # Alembic, cola Redis y worker real
│   └── test_webhooks.py   # Firma HMAC, reintentos e idempotencia
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile             # Imagen multi-stage, no root, healthcheck
├── docker-compose.yml     # API + worker + Redis para desarrollo local
├── render.yaml            # Blueprint: web + PostgreSQL + Key Value
├── .dockerignore          # Excluye secretos y artefactos locales
└── README.md
```

---

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `IG_DATABASE_URL` | `sqlite:///./igscraper.db` | URL de SQLAlchemy |
| `IG_DEBUG` | `false` | Habilita `/docs` y `/redoc` |
| `IG_OPENAPI_ENABLED` | `false` | Publica `/openapi.json` sin habilitar la UI (útil en RapidAPI) |
| `IG_JWT_SECRET` | **obligatorio** | Secreto JWT (≥32 caracteres, aleatorio) |
| `IG_JWT_ALGORITHM` | `HS256` | Solo `HS256`, `HS384` o `HS512` |
| `IG_JWT_EXPIRES_SECONDS` | `900` | Vida del token (15 min) |
| `IG_JWT_ISSUER` | `instagram-scraper-api` | Claim `iss` |
| `IG_JWT_AUDIENCE` | `instagram-scraper-clients` | Claim `aud` |
| `IG_CLIENT_ID` | **obligatorio** | Cliente normal |
| `IG_CLIENT_SECRET_HASH` | **obligatorio** | Hash PBKDF2 del secreto |
| `IG_CLIENT_SCOPES` | lectura + escritura | Scopes del cliente normal |
| `IG_ADMIN_CLIENT_ID` | — | Cliente de plataforma |
| `IG_ADMIN_CLIENT_SECRET_HASH` | — | Hash del secreto de plataforma |
| `IG_ENCRYPTION_KEY` | **obligatorio** | Clave Fernet (44 caracteres) |
| `IG_RATE_LIMIT_PER_MINUTE` | `600` | Rate limit general |
| `IG_AUTH_RATE_LIMIT_PER_MINUTE` | `5` | Límite de `/auth/token` |
| `IG_JOBS_RATE_LIMIT_PER_MINUTE` | `10` | Límite de `POST /jobs` |
| `IG_REQUESTS_LIMIT` | `50000` | Cuota mensual |
| `IG_CONCURRENT_JOBS_LIMIT` | `10` | Jobs concurrentes y tamaño del pool |
| `IG_JOB_BACKEND` | `thread` | `thread` (en proceso) o `redis` (worker real; exige Redis) |
| `IG_JOB_QUEUE_STREAM` | `ig:jobs` | Stream de Redis Streams; `<stream>:dead` guarda fallos definitivos |
| `IG_JOB_CONSUMER_GROUP` | `ig-jobs` | Consumer group compartido por los workers |
| `IG_JOB_VISIBILITY_SECONDS` | `300` | Tras este tiempo, un mensaje no confirmado se recupera |
| `IG_JOB_MAX_ATTEMPTS` | `3` | Intentos antes de mover el job al dead letter |
| `IG_JOB_POLL_SECONDS` | `1` | Espera del worker cuando la cola está vacía |
| `IG_JOB_CLAIM_BATCH` | `10` | Mensajes reclamados por ciclo |
| `IG_WEBHOOK_TIMEOUT_SECONDS` | `5` | Timeout por intento de entrega |
| `IG_WEBHOOK_MAX_ATTEMPTS` | `3` | Intentos antes de marcar la entrega como fallida |
| `IG_WEBHOOK_BACKOFF_SECONDS` | `1` | Backoff base (1 s, 2 s, 4 s...) entre intentos |
| `IG_WEBHOOK_DELIVERY_BUDGET_SECONDS` | `10` | Tiempo máximo compartido por todas las entregas de un evento |
| `IG_MAX_BODY_BYTES` | `1000000` | Tamaño máximo del body |
| `IG_CORS_ORIGINS` | vacío | Orígenes permitidos, separados por coma |
| `IG_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Hosts aceptados por TrustedHost; obligatorio y sin `*` |
| `IG_FORCE_HTTPS` | `false` | Redirige a HTTPS y añade HSTS |
| `IG_VALIDATE_OUTBOUND_DNS` | `true` | Resuelve DNS al validar URLs de webhook/proxy |
| `IG_JOB_SIMULATION_SECONDS` | `3` | Duración simulada del job |
| `IG_JOB_STEPS` | `10` | Pasos de progreso |
| `IG_SCRAPER_BACKEND` | `simulated` | `simulated` o `bluesky` |
| `IG_PROFILE_CACHE_TTL_SECONDS` | `3600` | TTL de la caché de perfiles; `0` la desactiva |
| `IG_BLUESKY_API_BASE` | `https://public.api.bsky.app` | AppView público (lecturas) |
| `IG_BLUESKY_APPVIEW_BASE` | `https://api.bsky.app` | AppView para `searchPosts` sin credenciales |
| `IG_BLUESKY_PDS_URL` | `https://bsky.social` | PDS para la sesión autenticada |
| `IG_BLUESKY_HANDLE` | — | Handle de la cuenta (junto con la app password) |
| `IG_BLUESKY_APP_PASSWORD` | — | App password de Bluesky (secreto) |
| `IG_BLUESKY_TIMEOUT_SECONDS` | `20` | Timeout HTTP del adaptador |
| `IG_BLUESKY_MAX_RETRIES` | `3` | Reintentos ante 429/5xx/red |
| `IG_BLUESKY_BACKOFF_SECONDS` | `1` | Base del backoff exponencial |
| `IG_RATE_LIMIT_BACKEND` | `memory` | `memory` (single process) o `redis` (estado compartido) |
| `IG_REDIS_URL` | `redis://localhost:6379/0` | URL Redis; solo `redis://` o `rediss://` |
| `IG_REDIS_TIMEOUT_SECONDS` | `1` | Timeout de conexión y de socket a Redis |
| `IG_RATE_LIMIT_FAIL_OPEN` | `true` | Si Redis cae: `true` degrada sin bloquear; `false` rechaza. `/auth/token` siempre fail-closed |

---

## Solución de problemas

### Windows: DLL de SQLAlchemy bloqueada

Si al importar SQLAlchemy aparece:

```
ImportError: DLL load failed while importing _collections_cy: ...
```

es **Smart App Control / Control de aplicaciones** bloqueando la extensión
compilada del wheel. Instala SQLAlchemy en modo Python puro:

```powershell
$env:DISABLE_SQLALCHEMY_CEXT="1"
pip install --no-binary sqlalchemy --no-cache-dir --force-reinstall --no-deps "SQLAlchemy>=2.0,<3.0"
```

Verifica:

```powershell
python -c "import sqlalchemy; print(sqlalchemy.__version__)"
```

### La app no arranca: falta un secreto

Es el comportamiento esperado (fail-fast). Revisa que `.env` tenga
`IG_JWT_SECRET`, `IG_ENCRYPTION_KEY`, `IG_CLIENT_ID` e `IG_CLIENT_SECRET_HASH`.
Los secretos de ejemplo del repositorio se rechazan a propósito.

---

## Próximos pasos sugeridos

Ya implementados: worker real sobre Redis Streams, migraciones Alembic a
PostgreSQL y entrega de webhooks firmados (`X-Signature` HMAC-SHA256).

1. Añadir TTL a la caché de hashtags y posts (la de perfiles ya lo tiene).
2. Reenviar entregas de webhook fallidas desde un barrido periódico (hoy se
   reintentan de forma inmediata con backoff y quedan registradas como `failed`).
3. Si se necesita Instagram de verdad, añadir un backend `instagrapi` o de
   proveedor detrás del mismo `ScraperBackend`; el resto de la API no cambia.
