"""Owner-controlled automatic rotation of the API link's secret path."""

from __future__ import annotations

import asyncio

import pytest

from llmop.apilink import ApiLink, RotationError, to_seconds
from llmop.store import Store
from llmop.web import build_web_router

from test_web_v2 import ALLOWED_PUBLIC_WRITES, login, make_client, make_state, routes_of

H, D = 3600.0, 86400.0


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def link(tmp_path, clock):
    store = Store(tmp_path / "llmop.sqlite3")
    yield ApiLink(store, tmp_path, clock=clock)
    store.close()


def run(coro):
    return asyncio.run(coro)


def test_to_seconds_units_and_decimals():
    assert to_seconds(3.5, "days") == 3.5 * D
    assert to_seconds(2, "hours") == 2 * H
    for bad in (("x", "days"), (-1, "days"), (1, "weeks"), (True, "days"), (10**6, "days")):
        with pytest.raises(RotationError):
            to_seconds(*bad)


def test_settings_validation_grace_must_be_shorter(link):
    with pytest.raises(RotationError) as e:
        link.set_rotation(1 * D, 1 * D)
    assert "寬限期" in str(e.value)
    with pytest.raises(RotationError):
        link.set_rotation(1 * H, 2 * H)
    link.set_rotation(1 * D, 1 * H)  # fine
    link.set_rotation(0, 5 * D)  # off: grace is not compared


def test_save_resets_timer_and_persists(tmp_path, clock):
    store = Store(tmp_path / "x.sqlite3")
    al = ApiLink(store, tmp_path, clock=clock)
    assert al.next_rotation_at() is None
    al.set_rotation(3.5 * D, 1 * H)
    assert al.next_rotation_at() == clock.t + 3.5 * D
    clock.t += D
    al.set_rotation(2 * D, 0)  # a change restarts from now
    assert al.next_rotation_at() == clock.t + 2 * D
    al2 = ApiLink(store, tmp_path, clock=clock)  # "restart"
    assert al2.rotation_settings() == {"interval": 2 * D, "grace": 0.0}
    assert al2.next_rotation_at() == clock.t + 2 * D
    al.set_rotation(0, 0)
    assert al.next_rotation_at() is None and al.rotation_info()["enabled"] is False
    store.close()


def test_extend_normal_overdue_and_repeatable(link, clock):
    with pytest.raises(RotationError):
        link.extend_rotation(D)  # off
    link.set_rotation(1 * D, 0)
    start = link.next_rotation_at()
    link.extend_rotation(2 * H)
    link.extend_rotation(2 * H)
    assert link.next_rotation_at() == start + 4 * H
    clock.t = start + 10 * D  # long overdue: counts from now
    link.extend_rotation(1 * D)
    assert link.next_rotation_at() == clock.t + 1 * D
    with pytest.raises(RotationError):
        link.extend_rotation(0)


def test_any_rotation_resets_timer_and_uses_configured_grace(link, clock):
    link.set_rotation(2 * D, 1 * H)
    clock.t += 1 * D
    old = link.gate
    res = link.rotate_path("manual")  # default grace = configured grace
    assert link.next_rotation_at() == clock.t + 2 * D
    assert res["grace_until"] == clock.t + 1 * H and link.gate != old
    info = link.rotation_info()
    assert info["old_link_valid_until"] == clock.t + 1 * H and info["created_at"] == clock.t
    clock.t += 3600 * 3
    link.rotate_path("cli", 0.0)  # explicit CLI grace 0 also resets
    assert link.next_rotation_at() == clock.t + 2 * D
    assert link.rotation_info()["old_link_valid_until"] is None


def test_scheduler_rotates_only_when_due(link, clock):
    assert run(link.rotation_tick()) is False  # off
    link.set_rotation(1 * D, 1 * H)
    old = link.gate
    clock.t += 1 * D - 1
    assert run(link.rotation_tick()) is False and link.gate == old
    clock.t += 1
    assert run(link.rotation_tick()) is True
    assert link.gate != old
    assert link.next_rotation_at() == clock.t + 1 * D
    assert link.rotation_info()["old_link_valid_until"] == clock.t + 1 * H
    assert run(link.rotation_tick()) is False


def test_scheduler_rotates_after_downtime(tmp_path, clock):
    store = Store(tmp_path / "x.sqlite3")
    a = ApiLink(store, tmp_path, clock=clock)
    a.set_rotation(1 * D, 0)
    old = a.gate
    clock.t += 5 * D  # node was off past the deadline
    b = ApiLink(store, tmp_path, clock=clock)
    assert b.gate == old
    assert run(b.rotation_tick()) is True and b.gate != old
    store.close()


def test_tick_initialises_missing_deadline(link, clock):
    from llmop.apilink import ROTATION_KV

    link.store.kv_set(ROTATION_KV, '{"interval": 86400, "grace": 0}')
    assert run(link.rotation_tick()) is False
    assert link.next_rotation_at() == clock.t + D


# ---------------------------------------------------------------- console endpoints
def web(link_obj=None):
    state = make_state()
    state.api_link = link_obj or ApiLink(state.store, None, clock=Clock())
    c = make_client(state, "local")
    return state, c, {"X-CSRF-Token": login(c, "root")}


def test_endpoints_validate_save_extend_and_rotate():
    state, c, h = web()
    url = "/ui/api/admin/api-link/rotation"
    info = c.get(url).json()
    assert info["enabled"] is False and info["next_rotation_at"] is None
    assert c.post(url, json={"interval": 1, "unit": "days"}).status_code == 403  # CSRF
    r = c.post(url, json={"interval": 1, "unit": "days", "grace": 1, "grace_unit": "days"}, headers=h)
    assert r.status_code == 400 and "寬限期" in r.json()["error"]["message"]
    for bad in ({"interval": "x", "unit": "days"}, {"interval": -1, "unit": "days"},
                {"interval": 1, "unit": "weeks"}):
        assert c.post(url, json=bad, headers=h).status_code == 400
    r = c.post(url, json={"interval": 3.5, "unit": "days", "grace": 2, "grace_unit": "hours"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interval_seconds"] == 3.5 * D and body["grace_seconds"] == 2 * H
    assert body["next_rotation_at"] == body["now"] + 3.5 * D
    r = c.post(url + "/extend", json={"amount": 12, "unit": "hours"}, headers=h)
    assert r.json()["next_rotation_at"] == body["next_rotation_at"] + 12 * H
    assert c.post(url + "/extend", json={"amount": 0, "unit": "hours"}, headers=h).status_code == 400
    # rotate-now (existing route) uses the configured grace and restarts the timer
    state.api_link.clock.t += 1 * D
    r = c.post("/ui/api/admin/api-link/rotate", json={}, headers=h)
    assert r.status_code == 200
    got = c.get(url).json()
    assert got["next_rotation_at"] == state.api_link.clock.t + 3.5 * D
    assert got["old_link_valid_until"] == state.api_link.clock.t + 2 * H


def test_endpoints_unavailable_without_link_and_for_non_admin():
    state = make_state()
    c = make_client(state, "local")
    login(c, "root")
    assert c.get("/ui/api/admin/api-link/rotation").status_code == 503
    c2 = make_client(make_state(), "local")
    login(c2, "alice")
    assert c2.get("/ui/api/admin/api-link/rotation").status_code == 403


def test_endpoints_absent_on_public_surface():
    state, _, _ = web()
    pub = make_client(state, "public")
    login(pub, "root")
    for path in ("/ui/api/admin/api-link/rotation", "/ui/api/admin/api-link/rotation/extend"):
        assert pub.get(path).status_code in (404, 405)
        assert pub.post(path, json={}).status_code in (404, 405)
    routes = routes_of(build_web_router(state, "public"))
    assert not any("api-link/rotation" in p for _, p in routes)
    assert ("POST", "/ui/api/admin/api-link/rotation") not in ALLOWED_PUBLIC_WRITES
