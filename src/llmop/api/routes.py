"""The OpenAI-compatible ``/v1`` routes.

``build_api_router(state, zone)`` (DISPATCH T-6):

* ``zone="local"`` - the key-less local entrance: every request runs as the config's
  ``local_account``; streaming on by default.
* ``zone="public"`` - API key required; keys marked ``encryption: required`` get 403
  (they only work through the encrypted relay); ``stream: true`` is answered with one
  complete SSE chunk unless public streaming is enabled (TIERS 7.5).
* ``zone=None`` (default) - the v0.1 behaviour (API key, streaming, error details).

Both zones: the single configured ``model``, the scheduler's queue, the usage tables,
a 4 MiB request body limit and no backend error details in responses.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..auth import ApiCaller, AuthError, authenticate_api, client_ip, key_allows_plaintext
from ..backends import ModelInfo
from ..config import Account, Config
from .errors import ApiError, openai_error
from .proxy import forward, json_response, model_permitted

if TYPE_CHECKING:
    from ..app import AppState

log = logging.getLogger("llmop.api")

MAX_BODY_BYTES = 4 * 1024 * 1024  # request body limit (DISPATCH T-6)
ZONES = ("local", "public")

SUPPORTED = (
    "GET /v1/models, GET /v1/models/{model}, POST /v1/chat/completions, "
    "POST /v1/completions, POST /v1/embeddings, GET /v1/me"
)


class OpenAIRoute(APIRoute):
    """Renders errors from ``/v1`` handlers as OpenAI-style JSON. Router-local, so the
    web UI (which shares the app) keeps its own error handling."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except ApiError as exc:
                return exc.response()
            except AuthError as exc:
                return openai_error(exc.status, exc.message, code=exc.code)
            except HTTPException as exc:
                return openai_error(exc.status_code, str(exc.detail))
            except RequestValidationError as exc:
                return openai_error(400, f"Invalid request: {exc.errors()}")
            except Exception:
                log.exception("unhandled error in %s %s", request.method, request.url.path)
                return openai_error(500, "Internal server error.", code="internal_error")

        return route_handler


def _too_large(limit: int) -> ApiError:
    return ApiError(413, f"請求內容太大，上限 {limit // (1024 * 1024)} MiB "
                    f"(the request body is larger than {limit // (1024 * 1024)} MiB).",
                    code="request_too_large")


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The body, refused with 413 above ``limit`` bytes: ``Content-Length`` is checked
    first, then the bytes are counted while reading (chunked / lying clients)."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > limit:
                raise _too_large(limit)
        except ValueError:
            raise ApiError(400, "Invalid Content-Length header.", code="invalid_request") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: Request, limit: int | None = None) -> Any:
    raw = await (request.body() if limit is None else read_body(request, limit))
    if not raw.strip():
        raise ApiError(400, "The request body is empty; expected a JSON object.", code="invalid_json")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError; too deep -> 400
        raise ApiError(
            400, "We could not parse the JSON body of your request. Send a JSON object "
            "with 'Content-Type: application/json'.", code="invalid_json",
        ) from None


def model_object(model_id: str, info: ModelInfo, created: int) -> dict:
    obj: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": info.created or created,
        "owned_by": info.owned_by,
    }
    if info.context_length:
        obj["context_length"] = info.context_length
    return obj


async def visible_models(state: AppState, account: Account) -> list[dict]:
    """OpenAI model objects the account may use: backend models (deduplicated by
    id, first backend wins - same as routing) plus usable ``model_aliases``."""
    cfg = state.config
    created = int(state.started_at)
    models = await state.router.list_models()
    seen: set[str] = set()
    out: list[dict] = []
    for m in models:
        if m.id in seen or not model_permitted(account, cfg, m.id, m.backend, m.id):
            continue
        seen.add(m.id)
        out.append(model_object(m.id, m, created))
    for alias, target in cfg.model_aliases.items():
        if alias in seen:
            continue
        hit = state.router.lookup(models, target)
        if hit is not None and model_permitted(account, cfg, alias, hit.backend, hit.id):
            seen.add(alias)
            out.append(model_object(alias, hit, created))
    return out


def single_model_object(state: AppState, model: str, models: list | None = None) -> dict:
    """The one entry of ``/v1/models`` when ``config.model`` is set."""
    hit = state.router.lookup(models, model) if models else None
    info = hit or ModelInfo(id=model, backend="", owned_by="llmop")
    return model_object(model, info, int(state.started_at))


def _tables(state: AppState) -> Any:
    usage = getattr(state, "usage", None)
    return usage if usage is not None else getattr(getattr(state, "quota", None), "tables", None)


def build_api_router(state: AppState, zone: str | None = None, *,
                     public_stream: bool | None = None) -> APIRouter:
    """``zone``: ``"local"``, ``"public"`` or None (v0.1 behaviour, see module doc).

    ``public_stream`` lets the public zone stream; None reads
    ``config.server.public_stream`` (default off) on each request."""
    if zone is not None and zone not in ZONES:
        raise ValueError(f"zone must be one of {ZONES}, got {zone!r}")
    router = APIRouter(prefix="/v1", route_class=OpenAIRoute)

    def authenticate(request: Request) -> tuple[Config, ApiCaller]:
        cfg = state.config  # per request: follows config reloads
        if zone == "local":
            name = getattr(cfg, "local_account", "") or ""
            account = cfg.account(name) if name else None
            if account is None:
                raise ApiError(503, "本機入口沒有設定帳號 (no local_account is configured).",
                               code="local_account_missing")
            if account.disabled:
                raise AuthError("This account is disabled.", code="account_disabled", status=403)
            return cfg, ApiCaller(account=account, key=None)  # type: ignore[arg-type]
        caller = authenticate_api(request, cfg, state.store, allow_guest=True,
                                  usage=_tables(state))
        if not key_allows_plaintext(caller.key, caller.account):
            raise ApiError(403, "這把 key 只能用於加密模式 (this API key only works through the "
                           "encrypted relay, /e2e/v1).", code="encryption_required")
        return cfg, caller

    def streaming_allowed(cfg: Config) -> bool:
        if zone != "public":
            return True
        if public_stream is not None:
            return public_stream
        return bool(getattr(cfg.server, "public_stream", False))

    async def proxy(request: Request, endpoint: str) -> Response:
        cfg, caller = authenticate(request)
        body = await read_json(request, MAX_BODY_BYTES if zone else None)
        key = caller.key
        return await forward(
            state, endpoint, body, account=caller.account, key_id=key.id if key else None,
            client_ip=client_ip(request, cfg), key=key, zone=zone,
            stream_ok=streaming_allowed(cfg),
        )

    async def models_for(caller: ApiCaller) -> list[dict]:
        single = (getattr(state.config, "model", "") or "").strip()
        if not single:
            if caller.account is None:
                return []
            return await visible_models(state, caller.account)
        try:
            models = await state.router.list_models()
        except Exception:
            log.debug("model listing failed", exc_info=True)
            models = []
        return [single_model_object(state, single, models)]

    @router.get("/models")
    async def list_models(request: Request) -> Response:
        _, caller = authenticate(request)
        return json_response({"object": "list", "data": await models_for(caller)})

    @router.get("/models/{model_id:path}")
    async def retrieve_model(request: Request, model_id: str) -> Response:
        cfg, caller = authenticate(request)
        for obj in await models_for(caller):
            if obj["id"] == model_id:
                return json_response(obj)
        if (getattr(cfg, "model", "") or "").strip() or caller.account is None:
            raise ApiError(404, f"The model '{model_id}' does not exist or is not available.",
                           code="model_not_found", param="model")
        # Also accept "<backend>/<model>" and ":latest" spellings that routing accepts.
        hit = state.router.lookup(await state.router.list_models(), model_id)
        if hit is not None and model_permitted(caller.account, cfg, model_id, hit.backend, hit.id):
            return json_response(model_object(model_id, hit, int(state.started_at)))
        raise ApiError(404, f"The model '{model_id}' does not exist or is not available.",
                       code="model_not_found", param="model")

    @router.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await proxy(request, "chat.completions")

    @router.post("/completions")
    async def completions(request: Request) -> Response:
        return await proxy(request, "completions")

    @router.post("/embeddings")
    async def embeddings(request: Request) -> Response:
        return await proxy(request, "embeddings")

    @router.get("/me")
    async def me(request: Request) -> Response:
        cfg, caller = authenticate(request)
        acc, key = caller.account, caller.key
        out: dict[str, Any] = {"object": "account"}
        if acc is not None:
            out.update({
                "username": acc.username,
                "display_name": acc.display_name or acc.username,
                "role": acc.role,
                "models": list(acc.models),
                "quota": state.quota.usage(acc),
            })
        else:  # GUEST key
            out.update({"username": None, "display_name": key.label or key.name,
                        "role": "guest", "models": [], "quota": None})
        out["key"] = None if key is None else {
            "id": key.id,
            "name": key.name,
            "display": key.display,
            "created_at": key.created_at,
            "last_used_at": key.last_used_at,
            "expires_at": key.expires_at,
            "encryption": getattr(key, "encryption", "optional"),
        }
        if zone is not None:
            out.update(usage_info(state, acc, key))
            out["zone"] = zone
            single = (getattr(cfg, "model", "") or "").strip()
            if single:
                out["models"] = [single]
        return json_response(out)

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False)
    async def unknown(request: Request, path: str) -> Response:
        raise ApiError(404, f"Unknown API endpoint: {request.method} /v1/{path}. "
                       f"Supported: {SUPPORTED}.", code="unknown_url")

    return router


def usage_info(state: AppState, account: Account | None, key: Any) -> dict:
    """``/v1/me`` additions (TIERS 7.4 / 10): tier, NORMAL/LOW, both usage tables
    (or the GUEST budget) and the caller's place in the queue."""
    tables = _tables(state)
    status: dict | None = None
    if tables is not None:
        try:
            status = tables.status(account, key if key is not None and key.is_guest else None)
        except Exception:
            log.exception("usage status failed")
    tier = (status or {}).get("tier") or (getattr(account, "tier", None) if account else "guest")
    info: dict[str, Any] = {
        "tier": tier,
        "status": (status or {}).get("state") or "NORMAL",
        "usage": None if status is None else {
            "bucket": status.get("bucket"),
            "weekly": status.get("weekly"),
            "guest": status.get("guest"),
            "retry_after": status.get("retry_after"),
        },
    }
    sched = getattr(state, "scheduler", None)
    if sched is None:
        info["queue"] = None
        return info
    snap = sched.snapshot()
    name = account.username if account is not None else f"guest:{key.id}"
    mine = snap["accounts"].get(name) or {}
    eta = mine.get("eta")
    info["queue"] = {
        "position": mine.get("position"),
        "eta": None if eta is None else math.ceil(eta),
        "running": mine.get("running", 0),
        "waiting": mine.get("waiting", 0),
        "total_waiting": snap["waiting"],
        "total_running": snap["running"],
        "slots": snap["slots"],
    }
    return info
