"""OpenAI-style errors: ``{"error": {"message", "type", "param", "code"}}``."""

from __future__ import annotations

from collections.abc import Mapping

from starlette.responses import JSONResponse

from ..backends import BackendError


def default_type(status: int) -> str:
    if status == 403:
        return "permission_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


def error_body(
    message: str,
    type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict:
    return {"error": {"message": message, "type": type, "param": param, "code": code}}


def openai_error(
    status: int,
    message: str,
    type: str | None = None,
    code: str | None = None,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        error_body(message, type or default_type(status), code, param),
        status_code=status,
        headers=dict(headers) if headers else None,
    )


class ApiError(Exception):
    """Raise anywhere in the API layer; rendered with :func:`openai_error`."""

    def __init__(
        self,
        status: int,
        message: str,
        type: str | None = None,
        code: str | None = None,
        param: str | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type or default_type(status)
        self.code = code
        self.param = param
        self.headers = dict(headers or {})

    @classmethod
    def from_backend(cls, exc: BackendError) -> "ApiError":
        return cls(exc.status, exc.message, exc.type, exc.code, exc.param)

    def response(self, extra_headers: Mapping[str, str] | None = None) -> JSONResponse:
        return openai_error(
            self.status, self.message, self.type, self.code, self.param,
            {**self.headers, **(extra_headers or {})},
        )
