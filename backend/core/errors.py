"""RFC 9457 Problem Details 统一错误处理 — backend-api-spec §2 全局约定."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_STATUS_TITLES = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 409: "Conflict", 422: "Validation Failed",
    429: "Too Many Requests", 500: "Internal Server Error",
}


class ProblemError(Exception):
    """业务侧主动抛出的 Problem Details 错误（带项目扩展 error_code）。"""

    def __init__(self, status: int, detail: str, error_code: str,
                 *, title: str | None = None, headers: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.title = title or _STATUS_TITLES.get(status, "Error")
        self.detail = detail
        self.error_code = error_code
        self.headers = headers


def problem_response(
    status: int,
    title: str,
    detail: str,
    instance: str,
    error_code: str,
    type_uri: str = "about:blank",
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": type_uri,
            "title": title,
            "status": status,
            "detail": detail,
            "instance": instance,
            "error_code": error_code,
        },
        media_type="application/problem+json",
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        detail = "; ".join(
            filter(None, [str(first.get("loc", "")), first.get("msg", "")])
        )
        return problem_response(
            422, "Validation Failed", detail or "invalid request",
            request.url.path, "VALIDATION_ERROR",
            "https://docs.patterntrace.app/errors/validation",
        )

    @app.exception_handler(PermissionError)
    async def forbidden_handler(request: Request, exc: PermissionError):
        return problem_response(
            403, "Forbidden", str(exc), request.url.path, "FORBIDDEN",
        )

    @app.exception_handler(ProblemError)
    async def problem_error_handler(request: Request, exc: ProblemError):
        return problem_response(
            exc.status, exc.title, exc.detail, request.url.path,
            exc.error_code, headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # BE-41：500 也必须落 problem+json（含项目扩展 error_code）
        return problem_response(
            500, _STATUS_TITLES[500], "internal server error",
            request.url.path, "INTERNAL_ERROR")
