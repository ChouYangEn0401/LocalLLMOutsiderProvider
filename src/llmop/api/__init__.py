"""The OpenAI-compatible API layer (``/v1/*``, API-key auth)."""

from .errors import ApiError, openai_error
from .proxy import ENDPOINTS, forward, model_permitted
from .routes import build_api_router, visible_models

__all__ = [
    "ENDPOINTS",
    "ApiError",
    "build_api_router",
    "forward",
    "model_permitted",
    "openai_error",
    "visible_models",
]
