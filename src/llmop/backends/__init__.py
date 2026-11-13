"""LLM backends (Ollama, vLLM, LM Studio, llama.cpp, any OpenAI-compatible server)."""

from .base import (
    ENDPOINT_PATHS,
    BackendAdapter,
    BackendError,
    ModelInfo,
    ModelNotFound,
    OpenAICompatAdapter,
    find_model,
)
from .ollama import OllamaAdapter
from .router import BackendRouter, make_adapter

__all__ = [
    "ENDPOINT_PATHS",
    "BackendAdapter",
    "BackendError",
    "BackendRouter",
    "ModelInfo",
    "ModelNotFound",
    "OllamaAdapter",
    "OpenAICompatAdapter",
    "find_model",
    "make_adapter",
]
