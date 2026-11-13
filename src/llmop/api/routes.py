"""The OpenAI-compatible ``/v1`` routes (API-key auth only)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..auth import ApiCaller, AuthError, authenticate_api, client_ip
from ..backends import ModelInfo
from ..config import Account, Config
from .errors import ApiError, openai_error
from .proxy import forward, json_response, model_permitted

if TYPE_CHECKING:
    from ..app import AppState

log = logging.getLogger("llmop.api")

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


async def read_json(request: Request) -> Any:
    raw = await request.body()
    if not raw.strip():
        raise ApiError(400, "The request body is empty; expected a JSON object.", code="invalid_json")
    try:
        return json.loads(raw)
    except ValueError:  # includes UnicodeDecodeError
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


def build_api_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/v1", route_class=OpenAIRoute)

    def authenticate(request: Request) -> tuple[Config, ApiCaller]:
        cfg = state.config  # per request: follows config reloads
        return cfg, authenticate_api(request, cfg, state.store)

    async def proxy(request: Request, endpoint: str) -> Response:
        cfg, caller = authenticate(request)
        body = await read_json(request)
        return await forward(
            state, endpoint, body, account=caller.account, key_id=caller.key.id,
            client_ip=client_ip(request, cfg),
        )

    @router.get("/models")
    async def list_models(request: Request) -> Response:
        _, caller = authenticate(request)
        return json_response({"object": "list", "data": await visible_models(state, caller.account)})

    @router.get("/models/{model_id:path}")
    async def retrieve_model(request: Request, model_id: str) -> Response:
        cfg, caller = authenticate(request)
        for obj in await visible_models(state, caller.account):
            if obj["id"] == model_id:
                return json_response(obj)
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
        _, caller = authenticate(request)
        acc, key = caller.account, caller.key
        return json_response({
            "object": "account",
            "username": acc.username,
            "display_name": acc.display_name or acc.username,
            "role": acc.role,
            "models": list(acc.models),
            "quota": state.quota.usage(acc),
            "key": {
                "id": key.id,
                "name": key.name,
                "display": key.display,
                "created_at": key.created_at,
                "last_used_at": key.last_used_at,
                "expires_at": key.expires_at,
            },
        })

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False)
    async def unknown(request: Request, path: str) -> Response:
        raise ApiError(404, f"Unknown API endpoint: {request.method} /v1/{path}. "
                       f"Supported: {SUPPORTED}.", code="unknown_url")

    return router
