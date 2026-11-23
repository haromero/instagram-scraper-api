"""Tests de la revocación de tokens con store memoria/Redis (sin servidor)."""

import time

import jwt
import pytest

from app import revocation
from app.config import Settings, get_settings
from app.revocation import (
    InMemoryRevocationStore,
    RedisRevocationStore,
    build_revocation_store,
)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.fail = False
        self.ttls: list[int] = []

    def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis caído")
        self.ttls.append(ex)
        self.data[key] = (value, time.time() + ex)

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis caído")
        expiry = self.data.get(key, (None, 0))[1]
        return int(expiry > time.time())


def test_memory_store_revokes_until_expiry():
    store = InMemoryRevocationStore()
    store.revoke("jti-1", time.time() + 60)
    assert store.is_revoked("jti-1")
    assert not store.is_revoked("otro")

    store.revoke("jti-2", time.time() - 1)
    assert not store.is_revoked("jti-2")


def test_two_redis_stores_share_revocations():
    fake = FakeRedis()
    writer = RedisRevocationStore(fake)
    reader = RedisRevocationStore(fake)

    writer.revoke("jti-1", time.time() + 60)
    assert reader.is_revoked("jti-1")


def test_redis_revocation_expires():
    fake = FakeRedis()
    store = RedisRevocationStore(fake)

    store.revoke("jti-1", time.time() + 1)
    assert fake.ttls and fake.ttls[0] in (1, 2)
    time.sleep(1.1)
    assert not store.is_revoked("jti-1")


def test_redis_failure_fails_open_by_default():
    fake = FakeRedis()
    fake.fail = True
    store = RedisRevocationStore(fake, fail_open=True)
    assert not store.is_revoked("jti-1")


def test_redis_failure_can_fail_closed():
    fake = FakeRedis()
    fake.fail = True
    store = RedisRevocationStore(fake, fail_open=False)
    assert store.is_revoked("jti-1")


def test_build_revocation_store_selects_backend(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    assert isinstance(build_revocation_store(settings), InMemoryRevocationStore)

    fake = FakeRedis()
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    store = build_revocation_store(settings, client=fake)
    assert isinstance(store, RedisRevocationStore)
    store.revoke("jti-1", time.time() + 60)
    assert store.is_revoked("jti-1")


def test_module_level_compat_functions_use_configured_store():
    original = revocation._store
    fake = FakeRedis()
    try:
        revocation.configure(RedisRevocationStore(fake))
        revocation.revoke("jti-1", time.time() + 60)
        assert revocation.is_revoked("jti-1")
    finally:
        revocation.configure(original)


def test_invalid_redis_url_is_rejected():
    with pytest.raises(ValueError):
        Settings(redis_url="localhost:6379")


def test_token_revoked_by_one_worker_is_rejected_by_another():
    """Resultado esperado: revocar en un worker invalida el JWT en todos."""
    from app.security import create_access_token, decode_token

    fake = FakeRedis()
    writer = RedisRevocationStore(fake)
    reader = RedisRevocationStore(fake)
    token, expires_in = create_access_token("cli_revocado", ["profiles:read"])

    # El otro worker todavía lo acepta y ve el mismo jti.
    payload = decode_token(token)
    jti = payload["jti"]

    writer.revoke(jti, time.time() + expires_in)
    original = revocation._store
    try:
        revocation.configure(reader)
        assert revocation.is_revoked(jti)
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(token)
    finally:
        revocation.configure(original)
