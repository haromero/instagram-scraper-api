"""Esquemas Pydantic: contrato de entrada/salida de la API."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .config import get_settings
from .validators import assert_safe_outbound_url, assert_safe_proxy_url


# ---------------------------------------------------------------------------
# Comunes
# ---------------------------------------------------------------------------
class Error(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: Error


class Pagination(BaseModel):
    next_cursor: str | None = None
    has_more: bool
    limit: int


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class AuthTokenRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    client_secret: str = Field(min_length=1, max_length=256)


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scopes: list[str]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class JobType(str, Enum):
    profile = "profile"
    profile_posts = "profile_posts"
    profile_followers = "profile_followers"
    profile_following = "profile_following"
    post_comments = "post_comments"
    hashtag_posts = "hashtag_posts"
    search = "search"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class JobPriority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"


class WebhookEvent(str, Enum):
    job_completed = "job.completed"
    job_failed = "job.failed"
    job_cancelled = "job.cancelled"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class JobTarget(BaseModel):
    username: str | None = Field(default=None, max_length=253)
    shortcode: str | None = Field(default=None, max_length=300)
    hashtag: str | None = Field(default=None, max_length=128)
    query: str | None = Field(default=None, max_length=256)


class JobOptions(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    since: datetime | None = None
    until: datetime | None = None
    include_comments: bool = False
    mode: Literal["top", "recent"] | None = None


class JobCreateRequest(BaseModel):
    type: JobType
    target: JobTarget
    options: JobOptions = Field(default_factory=JobOptions)
    webhook_url: HttpUrl | None = None
    priority: JobPriority = JobPriority.normal

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None:
            assert_safe_outbound_url(
                str(value), resolve=get_settings().validate_outbound_dns
            )
        return value


class JobProgress(BaseModel):
    items_scraped: int = 0
    items_total: int = 0
    percent: int = 0


class Job(BaseModel):
    job_id: str
    type: JobType
    status: JobStatus
    progress: JobProgress
    result_url: str | None = None
    poll_url: str | None = None
    error: Error | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    estimated_completion: datetime | None = None


class JobList(BaseModel):
    data: list[Job]
    pagination: Pagination


class JobResult(BaseModel):
    job_id: str
    data: list[dict[str, Any]]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Perfiles y usuarios
# ---------------------------------------------------------------------------
class Profile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    username: str
    user_id: str | None = None
    full_name: str | None = None
    biography: str | None = None
    is_private: bool
    is_verified: bool = False
    followers_count: int = 0
    following_count: int = 0
    posts_count: int = 0
    profile_pic_url: str | None = None
    external_url: str | None = None
    scraped_at: datetime


class UserSummary(BaseModel):
    username: str
    user_id: str | None = None
    full_name: str | None = None
    is_verified: bool = False
    is_private: bool = False
    profile_pic_url: str | None = None


class UserSummaryList(BaseModel):
    data: list[UserSummary]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Posts y comentarios
# ---------------------------------------------------------------------------
class Post(BaseModel):
    id: str
    shortcode: str
    type: str
    caption: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    taken_at: datetime
    media_urls: list[str] = Field(default_factory=list)
    thumbnail_url: str | None = None
    owner: UserSummary | None = None
    scraped_at: datetime


class PostList(BaseModel):
    data: list[Post]
    pagination: Pagination


class Comment(BaseModel):
    id: str
    text: str
    author: UserSummary
    like_count: int = 0
    replies_count: int = 0
    created_at: datetime


class CommentList(BaseModel):
    data: list[Comment]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Stories y highlights
# ---------------------------------------------------------------------------
class Story(BaseModel):
    id: str
    media_type: str
    media_url: str
    thumbnail_url: str | None = None
    taken_at: datetime
    expires_at: datetime


class StoryList(BaseModel):
    data: list[Story]
    pagination: Pagination


class Highlight(BaseModel):
    id: str
    title: str
    cover_url: str | None = None
    item_count: int = 0
    updated_at: datetime


class HighlightList(BaseModel):
    data: list[Highlight]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Hashtags
# ---------------------------------------------------------------------------
class HashtagTopPost(BaseModel):
    shortcode: str
    like_count: int
    thumbnail_url: str | None = None


class Hashtag(BaseModel):
    name: str
    media_count: int
    top_posts: list[HashtagTopPost] = Field(default_factory=list)
    scraped_at: datetime


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------
class SearchUserResult(UserSummary):
    kind: Literal["user"] = "user"


class SearchHashtagResult(BaseModel):
    kind: Literal["hashtag"] = "hashtag"
    name: str
    media_count: int


SearchResult = Annotated[
    SearchUserResult | SearchHashtagResult,
    Field(discriminator="kind"),
]


class SearchResultList(BaseModel):
    data: list[SearchResult]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
class WebhookCreateRequest(BaseModel):
    url: HttpUrl
    events: list[WebhookEvent] = Field(min_length=1, max_length=20)
    secret: str | None = Field(default=None, max_length=256)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: HttpUrl) -> HttpUrl:
        assert_safe_outbound_url(
            str(value), resolve=get_settings().validate_outbound_dns
        )
        return value


class Webhook(BaseModel):
    webhook_id: str
    url: str
    events: list[WebhookEvent]
    active: bool = True
    created_at: datetime


class WebhookCreated(Webhook):
    """El secreto solo se devuelve al registrar; nunca aparece en el listado."""

    secret: str = Field(min_length=1, max_length=256)


class WebhookList(BaseModel):
    data: list[Webhook]
    pagination: Pagination


# ---------------------------------------------------------------------------
# Cuenta
# ---------------------------------------------------------------------------
class UsagePeriod(BaseModel):
    start: datetime
    end: datetime


class Usage(BaseModel):
    plan: str
    period: UsagePeriod
    requests_used: int
    requests_limit: int
    concurrent_jobs: int
    concurrent_jobs_limit: int
    reset_at: datetime


class EndpointLimit(BaseModel):
    requests_per_minute: int
    requests_per_day: int


class Limits(BaseModel):
    plan: str
    limits: dict[str, EndpointLimit]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class Health(BaseModel):
    status: str
    version: str


class HealthReady(BaseModel):
    status: str
    checks: dict[str, Any]
    version: str


# ---------------------------------------------------------------------------
# Admin: sesiones y proxies
# ---------------------------------------------------------------------------
class SessionCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    cookies: str = Field(min_length=2, max_length=20_000)
    proxy_id: str | None = Field(
        default=None, max_length=64, pattern=r"^proxy_[A-Za-z0-9]+$"
    )


class Session(BaseModel):
    session_id: str
    username: str
    status: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class SessionList(BaseModel):
    data: list[Session]
    pagination: Pagination


class ProxyCreateRequest(BaseModel):
    url: HttpUrl
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    country: str | None = Field(default=None, max_length=2)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: HttpUrl) -> HttpUrl:
        assert_safe_proxy_url(
            str(value), resolve=get_settings().validate_outbound_dns
        )
        return value


class Proxy(BaseModel):
    proxy_id: str
    url: str
    status: str
    country: str | None = None
    last_checked_at: datetime | None = None


class ProxyList(BaseModel):
    data: list[Proxy]
    pagination: Pagination
