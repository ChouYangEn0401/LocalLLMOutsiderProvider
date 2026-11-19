"""auth.authenticate_api with GUEST keys (TIERS 8): clear 401 codes, no account row,
usage logged as ``guest:<key id>``."""

from __future__ import annotations

import time

import pytest
from starlette.requests import Request

from llmop import security
from llmop.auth import AuthError, authenticate_api, usage_username
from llmop.config import parse_config
from llmop.store import Store
from llmop.usage import UsageManager

FAST_HASH = security.hash_password("pw", n=2**4)


@pytest.fixture
def env():
    cfg = parse_config({"accounts": {"alice": {"password_hash": FAST_HASH}}})
    store = Store(":memory:")
    yield cfg, store
    store.close()


def req(key: str | None) -> Request:
    headers = [(b"authorization", f"Bearer {key}".encode())] if key else []
    return Request({"type": "http", "method": "GET", "path": "/v1/me", "headers": headers})


def auth_error(fn) -> AuthError:
    with pytest.raises(AuthError) as info:
        fn()
    return info.value


def test_guest_key_authenticates_without_account(env):
    cfg, store = env
    plain, rec = store.create_guest_key("visitor")
    caller = authenticate_api(req(plain), cfg, store, allow_guest=True)
    assert caller.account is None and caller.is_guest and caller.key.id == rec.id
    assert caller.usage_name == f"guest:{rec.id}"


def test_guest_key_refused_where_not_allowed(env):
    cfg, store = env
    plain, _ = store.create_guest_key("visitor")
    err = auth_error(lambda: authenticate_api(req(plain), cfg, store))
    assert err.code == "guest_not_allowed"


def test_revoked_guest_key_is_key_revoked(env):
    cfg, store = env
    plain, rec = store.create_guest_key("visitor")
    store.revoke_api_key(rec.id)
    err = auth_error(lambda: authenticate_api(req(plain), cfg, store, allow_guest=True))
    assert (err.status, err.code) == (401, "key_revoked")


def test_expired_guest_key_is_key_expired(env):
    cfg, store = env
    plain, _ = store.create_guest_key("visitor", expires_at=time.time() - 5)
    err = auth_error(lambda: authenticate_api(req(plain), cfg, store, allow_guest=True))
    assert (err.status, err.code) == (401, "key_expired")


def test_exhausted_guest_key_is_key_exhausted(env):
    cfg, store = env
    plain, rec = store.create_guest_key("visitor", budget_tokens=100)
    um = UsageManager(store)
    um.charge(150, None, rec)
    err = auth_error(lambda: authenticate_api(req(plain), cfg, store, allow_guest=True,
                                              usage=um))
    assert (err.status, err.code) == (401, "key_exhausted")


def test_resetting_guest_key_used_up_still_authenticates(env):
    cfg, store = env
    plain, rec = store.create_guest_key("visitor", budget_tokens=100, reset=True)
    um = UsageManager(store)
    um.charge(150, None, rec)
    caller = authenticate_api(req(plain), cfg, store, allow_guest=True, usage=um)
    assert caller.account is None  # the quota answers 429 until the reset instead


def test_account_keys_unchanged(env):
    cfg, store = env
    plain, rec = store.create_api_key("alice", "k")
    caller = authenticate_api(req(plain), cfg, store, allow_guest=True)
    assert caller.account.username == "alice" and caller.usage_name == "alice"
    store.revoke_api_key(rec.id)
    err = auth_error(lambda: authenticate_api(req(plain), cfg, store, allow_guest=True))
    assert err.code == "invalid_api_key"
    assert auth_error(lambda: authenticate_api(req(None), cfg, store)).code == "invalid_api_key"


def test_usage_username():
    with pytest.raises(ValueError):
        usage_username(None, None)
