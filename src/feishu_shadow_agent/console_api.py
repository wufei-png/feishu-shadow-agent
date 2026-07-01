from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import LoadedConfig
from .console_security import bearer_token_valid, host_header_allowed, validate_console_bind_host
from .operator_query import OperatorQueryReadError, OperatorQueryService, OperatorQueryUnavailable
from .settings_catalog import settings_catalog
from .store.sqlite_store import SQLiteStore

_ASSET_REF_PATTERN = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")


def default_console_static_dir() -> Path:
    return Path(__file__).with_name("console_static")


def console_static_ready(static_dir: Path | None = None) -> bool:
    return not missing_console_static_assets(static_dir or default_console_static_dir())


def missing_console_static_assets(static_dir: Path | None = None) -> list[str]:
    resolved_static_dir = static_dir or default_console_static_dir()
    index_path = _index_path(resolved_static_dir)
    if not index_path.is_file():
        return ["index.html"]
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except OSError:
        return ["index.html"]
    asset_paths = sorted(set(_ASSET_REF_PATTERN.findall(index_text)))
    missing = [
        asset_path.lstrip("/")
        for asset_path in asset_paths
        if not (resolved_static_dir / asset_path.lstrip("/")).is_file()
    ]
    return missing


def create_console_app(
    *,
    loaded_config: LoadedConfig,
    store: SQLiteStore,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    static_dir: Path | None = None,
) -> FastAPI:
    validate_console_bind_host(host)
    app = FastAPI(title="Feishu Shadow Agent Operator Console")
    resolved_static_dir = static_dir or default_console_static_dir()

    @app.middleware("http")
    async def validate_host_header(request: Request, call_next):
        if not host_header_allowed(request.headers.get("host"), host=host, port=port):
            return _error_response(
                403,
                "forbidden_origin_or_host",
                "Host header is not allowed for this local console.",
            )
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            detail = exc.detail
            return _error_response(
                exc.status_code,
                str(detail["code"]),
                str(detail.get("message") or exc.detail),
                details=_details_dict(detail.get("details")),
            )
        return _error_response(exc.status_code, _status_code_name(exc.status_code), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            400,
            "validation_failed",
            "Request validation failed.",
            details={"errors": exc.errors()},
        )

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not bearer_token_valid(authorization, token):
            _raise_api_error(401, "unauthorized", "A valid bearer token is required.")

    def query_service() -> OperatorQueryService:
        return OperatorQueryService(store, policy_import_source=loaded_config.config)

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @api.get("/dashboard")
    def dashboard() -> dict[str, Any]:
        return query_service().dashboard_snapshot()

    @api.get("/settings/catalog")
    def settings_catalog_route() -> dict[str, Any]:
        return settings_catalog()

    @api.get("/settings/runtime")
    def settings_runtime() -> dict[str, Any]:
        return query_service().settings_runtime(loaded_config.config)

    @api.get("/messages/{message_id}/detail")
    def message_detail(message_id: str) -> dict[str, Any]:
        try:
            detail = query_service().message_detail(message_id)
        except OperatorQueryUnavailable as exc:
            _raise_api_error(503, "store_unavailable", str(exc))
        except OperatorQueryReadError as exc:
            _raise_api_error(500, "internal_error", str(exc))
        if detail is None:
            _raise_api_error(404, "not_found", f"Message not found: {message_id}")
        return detail

    @api.api_route("", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def api_not_found(path: str = "") -> None:
        _raise_api_error(404, "not_found", f"API route not found: /api/{path}".rstrip("/"))

    app.include_router(api)
    _install_static_routes(app, resolved_static_dir)
    return app


def _install_static_routes(app: FastAPI, static_dir: Path) -> None:
    index_path = _index_path(static_dir)
    assets_path = static_dir / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="console_assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        if not index_path.is_file():
            _raise_api_error(
                503,
                "store_unavailable",
                "Operator Console renderer assets are missing. Run `npm --prefix frontend/operator-console run build`.",
            )
        return FileResponse(index_path)

    @app.get("/{path:path}", include_in_schema=False)
    def spa_fallback(path: str) -> FileResponse:
        if path.startswith("api/"):
            _raise_api_error(404, "not_found", "API route not found.")
        if path.startswith("assets/"):
            _raise_api_error(404, "not_found", "Static asset not found.")
        return index()


def _index_path(static_dir: Path) -> Path:
    return static_dir / "index.html"


def _raise_api_error(status_code: int, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _details_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status_code_name(status_code: int) -> str:
    if status_code == 404:
        return "not_found"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden_origin_or_host"
    if status_code == 409:
        return "conflict"
    if status_code == 503:
        return "store_unavailable"
    if status_code == 400:
        return "validation_failed"
    return "internal_error" if status_code >= 500 else "validation_failed"
