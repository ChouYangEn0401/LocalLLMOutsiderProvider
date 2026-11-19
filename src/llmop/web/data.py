"""Read-side helpers of the web layer: portal data (tier, both usage tables, per-key
usage, API link, queue) and the admin overview, with masking for the public surface.

AppState fields added in v0.2 (``scheduler``, ``usage``, ``api_link``, ``hub``,
``tunnels``) are read with ``getattr`` and every one of them may be missing: the page
then shows "not available" instead of failing.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from ..store import ApiKey

log = logging.getLogger("llmop.web")

#: Columns a key record may expose. Hashes never leave the store.
_B64U_SIG_LEN = 32


# --------------------------------------------------------------------------- generic
def plain(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy of a value from another module (health rows, tunnel attrs)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth < 3 and isinstance(value, (list, tuple)):
        return [plain(v, depth + 1) for v in value]
    if depth < 3 and isinstance(value, dict):
        return {str(k): plain(v, depth + 1) for k, v in value.items()}
    return str(value)


def read_attr(obj: Any, name: str) -> Any:
    """``obj.name`` (called when it is a plain method); errors become None."""
    try:
        value = getattr(obj, name, None)
        if callable(value) and not isinstance(value, type):
            value = value()
        if inspect.isawaitable(value):  # async getters are not supported here
            close = getattr(value, "close", None)
            if close:
                close()
            return None
        return value
    except Exception:  # a broken optional component must not break the page
        return None


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# --------------------------------------------------------------------------- usage tables
def usage_manager(state):
    """The shared ``UsageManager`` (``state.usage`` or ``state.quota.tables``); a
    stand-alone one over the same store otherwise (all its state is in the DB)."""
    mgr = getattr(state, "usage", None) or getattr(getattr(state, "quota", None), "tables", None)
    if mgr is not None:
        return mgr
    from ..usage import UsageManager

    return UsageManager(state.store, state.holder)


def usage_status(state, account=None, key: ApiKey | None = None) -> dict | None:
    try:
        return plain(usage_manager(state).status(account, key))
    except Exception as exc:
        log.warning("usage status failed: %s", type(exc).__name__)
        return None


def week_start(state) -> float:
    try:
        from ..usage import last_week_reset

        mgr = usage_manager(state)
        return float(last_week_reset(time.time(), mgr.settings()))
    except Exception:
        return time.time() - 7 * 86400


def usage_by_key(store, key_ids: list[str], since: float = 0.0) -> dict[str, dict]:
    """``{key_id: {"requests", "total"}}`` since ``since`` (0 = all time)."""
    if not key_ids:
        return {}
    marks = ",".join("?" * len(key_ids))
    try:
        rows = store._all(
            "SELECT key_id, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS total"
            f" FROM usage_events WHERE key_id IN ({marks}) AND ts >= ? GROUP BY key_id",
            (*key_ids, float(since)),
        )
    except Exception as exc:  # pragma: no cover - store without the table
        log.warning("per-key usage failed: %s", type(exc).__name__)
        return {}
    return {r["key_id"]: {"requests": int(r["requests"]), "total": int(r["total"])} for r in rows}


def key_usage_map(state, keys: list[ApiKey]) -> dict[str, dict]:
    ids = [k.id for k in keys]
    total = usage_by_key(state.store, ids)
    week = usage_by_key(state.store, ids, week_start(state))
    zero = {"requests": 0, "total": 0}
    return {i: {"all": total.get(i, zero), "week": week.get(i, zero)} for i in ids}


# --------------------------------------------------------------------------- keys
def sig_key_id(public_b64u: str | None) -> str | None:
    if not public_b64u:
        return None
    try:
        from ..crypto import signing_key_id
        from ..protocol import b64u_decode

        return signing_key_id(b64u_decode(public_b64u))
    except Exception:  # cryptography missing or a bad stored value
        return None


def valid_sig_public_key(value: Any) -> str | None:
    """A 32-byte Ed25519 public key as b64u, or None. Raises ValueError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("not a string")
    value = value.strip()
    from ..protocol import ProtocolError, b64u_decode

    try:
        raw = b64u_decode(value)
    except ProtocolError:
        raise ValueError("not b64u") from None
    if len(raw) != _B64U_SIG_LEN:
        raise ValueError("wrong length")
    return value


def key_json(k: ApiKey, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "id": k.id,
        "name": k.name,
        "display": k.display,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
        "expires_at": k.expires_at,
        "revoked_at": k.revoked_at,
        "active": k.active,
        "expired": k.expires_at is not None and k.expires_at <= now,
        "encryption": getattr(k, "encryption", "optional"),
        "sig_public_key": getattr(k, "sig_public_key", None),
        "sig_key_id": sig_key_id(getattr(k, "sig_public_key", None)),
    }


def guest_key_json(state, k: ApiKey, now: float, *, masked: bool) -> dict:
    item = key_json(k, now)
    item.update({
        "label": k.label,
        "budget_tokens": k.budget_tokens,
        "reset": bool(k.reset),
        "status": usage_status(state, None, k),
    })
    if masked:
        item.pop("sig_public_key", None)
    return item


# --------------------------------------------------------------------------- API link / tunnels
def api_link_info(state) -> dict:
    """The current public API link (``<api-host>/<gate>``) from ``state.api_link``.

    Accepted shapes (T-9 provides the object): ``current_url()`` / ``url()`` methods
    or a ``url`` / ``current_url`` attribute."""
    link = getattr(state, "api_link", None)
    url = ""
    if link is not None:
        for name in ("current_url", "url"):
            value = read_attr(link, name)
            if isinstance(value, str):
                url = value
                break
    updated = None
    try:
        row = state.store.get_api_link()
        updated = row["updated_at"] if row else None
    except Exception:
        pass
    return {
        "available": link is not None,
        "url": url,
        "v1_base_url": (url.rstrip("/") + "/v1") if url else "",
        "e2e_base_url": (url.rstrip("/") + "/e2e/v1") if url else "",
        "updated_at": updated,
    }


_TUNNEL_ATTRS = ("url", "status", "last_error", "restarts", "started_at", "mode")


def _one_tunnel(obj, settings) -> dict:
    info: dict[str, Any] = {
        "enabled": bool(getattr(settings, "enabled", False)) if settings is not None else None,
        "mode": getattr(settings, "mode", None) if settings is not None else None,
        "running": obj is not None,
    }
    if obj is not None:
        for attr in _TUNNEL_ATTRS:
            value = read_attr(obj, attr)
            if value is not None or attr not in info:
                info[attr] = plain(value)
    return info


def tunnel_info(state, cfg) -> dict:
    """The legacy single tunnel (``state.tunnel`` / ``cfg.tunnel``)."""
    return _one_tunnel(getattr(state, "tunnel", None), cfg.tunnel)


def tunnels_info(state, cfg) -> dict:
    """Both v0.2 tunnels: ``ui`` (account website) and ``api`` (API link)."""
    objs = getattr(state, "tunnels", None)
    if not isinstance(objs, dict):
        objs = {}
    conf = getattr(cfg, "tunnels", None)
    out = {}
    for name in ("ui", "api"):
        obj = objs.get(name)
        if obj is None and name == "ui" and not objs:
            obj = getattr(state, "tunnel", None)
        out[name] = _one_tunnel(obj, getattr(conf, name, None))
    return out


def mask_tunnels(tunnels: dict) -> dict:
    return {n: {"running": t.get("running"), "status": t.get("status"),
                "has_error": bool(t.get("last_error"))} for n, t in tunnels.items()}


# --------------------------------------------------------------------------- queue / workers
def queue_info(state, username: str | None = None, *, include_accounts: bool = False) -> dict:
    sched = getattr(state, "scheduler", None)
    if sched is None:
        return {"available": False}
    try:
        snap = sched.snapshot()
    except Exception as exc:
        log.warning("scheduler snapshot failed: %s", type(exc).__name__)
        return {"available": False}
    slots = max(1, int(snap.get("slots") or 1))
    waiting = int(snap.get("waiting") or 0)
    avg = float(snap.get("average_duration") or 0)
    out = {
        "available": True,
        "slots": slots,
        "running": int(snap.get("running") or 0),
        "waiting": waiting,
        "max_length": snap.get("max_length"),
        "eta_seconds": round(waiting * avg / slots, 1),
        "mine": plain((snap.get("accounts") or {}).get(username)) if username else None,
    }
    if include_accounts:
        out["by_tier"] = plain(snap.get("by_tier"))
        out["accounts"] = plain(snap.get("accounts"))
    return out


def _hub_conn(state, worker_id: str):
    hub = getattr(state, "hub", None)
    workers = getattr(hub, "workers", None)
    if isinstance(workers, dict):
        return workers.get(worker_id)
    return None


def worker_json(state, w, *, masked: bool) -> dict:
    conn = _hub_conn(state, w.id)
    item = {
        "id": w.id,
        "name": w.name,
        "online": conn is not None,
        "pending": len(getattr(conn, "pending", ()) or ()) if conn is not None else 0,
        "created_at": w.created_at,
        "last_seen_at": w.last_seen_at,
        "revoked_at": w.revoked_at,
        "reg_expires_at": w.reg_expires_at,
    }
    if not masked:
        try:
            item["models"] = state.store.worker_models(w.id)
            item["keys"] = state.store.worker_keys(w.id)
        except Exception:
            item["models"], item["keys"] = [], []
    return item


def slots_info(cfg) -> dict:
    """Global slots vs the backends' parallel capacity (TIERS 7.1 / 10 warning)."""
    slots = int(getattr(getattr(cfg, "queue", None), "slots", 0) or 0)
    capacity = sum(int(getattr(b, "max_concurrency", 0) or 0) for b in cfg.backends)
    return {"slots": slots, "backend_capacity": capacity,
            "mismatch": bool(slots and capacity and slots > capacity)}
