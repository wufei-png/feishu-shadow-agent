from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .config import LoadedConfig
from .console_security import bearer_token_valid, host_header_allowed, validate_console_bind_host
from .dispatcher import Dispatcher
from .feishu.lark_cli import LarkCliClient
from .jsonl import JSONLLogger
from .operator_commands import DispatchReadbackMarker, OperatorCommandService
from .operator_query import OperatorQueryReadError, OperatorQueryService, OperatorQueryUnavailable
from .paths import resolve_relative_path
from .settings_catalog import settings_catalog
from .store.sqlite_store import SQLiteStore
from .types import ActionStatus, ApprovalStatus, TaskStatus

_ASSET_REF_PATTERN = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")
LOCAL_CONSOLE_ACTOR = "local_console"


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    command_id: str | None = None
    final_reply: str | None = None
    sent_message_id: str | None = None


class PolicyImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    replace: bool = False


class GlobalPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    p2p_auto_reply: bool | None = None
    unknown_group_auto_reply: bool | None = None
    bot_joined: bool | None = None
    reply_identity: str | None = None
    allow_user_fallback: bool | None = None
    resource_download: bool | None = None

    def changes(self) -> dict[str, Any]:
        return _policy_request_changes(
            self,
            (
                "p2p_auto_reply",
                "unknown_group_auto_reply",
                "bot_joined",
                "reply_identity",
                "allow_user_fallback",
                "resource_download",
            ),
        )


class ChatPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    name: str | None = None
    auto_reply: bool | None = None
    bot_joined: bool | None = None
    reply_identity: str | None = None
    allow_user_fallback: bool | None = None
    resource_download: bool | None = None

    def changes(self) -> dict[str, Any]:
        return _policy_request_changes(
            self,
            (
                "name",
                "auto_reply",
                "bot_joined",
                "reply_identity",
                "allow_user_fallback",
                "resource_download",
            ),
        )


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
    readback_marker: DispatchReadbackMarker | None = None,
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

    def command_service(*, needs_readback_marker: bool = False) -> OperatorCommandService:
        marker = None
        if needs_readback_marker:
            marker = readback_marker or _build_dispatch_readback_marker(
                loaded_config=loaded_config,
                store=store,
            )
        return OperatorCommandService(
            store,
            readback_marker=marker,
        )

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @api.get("/dashboard")
    def dashboard() -> dict[str, Any]:
        return query_service().dashboard_snapshot()

    @api.get("/approvals")
    def approvals(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: ApprovalStatus | None = None,
    ) -> list[dict[str, Any]]:
        return query_service().list_approvals(
            status=None if status is None else status.value,
            limit=limit,
            offset=offset,
        )

    @api.get("/approvals/{approval_id}")
    def approval_detail(approval_id: str) -> dict[str, Any]:
        detail = query_service().approval_detail(approval_id)
        if detail is None:
            _raise_api_error(404, "not_found", f"Approval not found: {approval_id}")
        return detail

    @api.get("/tasks")
    def tasks(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: TaskStatus | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return query_service().list_tasks(
            status=None if status is None else status.value,
            chat_id=chat_id,
            limit=limit,
            offset=offset,
        )

    @api.get("/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        detail = query_service().task_detail(task_id)
        if detail is None:
            _raise_api_error(404, "not_found", f"Task not found: {task_id}")
        return detail

    @api.get("/dispatch/actions")
    def dispatch_actions(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[list[ActionStatus] | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        statuses = None if status is None else tuple(item.value for item in status)
        return query_service().list_dispatch_actions(statuses=statuses, limit=limit, offset=offset)

    @api.get("/dispatch/actions/{action_id}")
    def dispatch_action_detail(action_id: int) -> dict[str, Any]:
        detail = query_service().dispatch_action_detail(action_id)
        if detail is None:
            _raise_api_error(404, "not_found", f"Dispatch action not found: {action_id}")
        return detail

    @api.get("/settings/catalog")
    def settings_catalog_route() -> dict[str, Any]:
        return settings_catalog()

    @api.get("/settings/runtime")
    def settings_runtime() -> dict[str, Any]:
        return query_service().settings_runtime(loaded_config.config)

    @api.get("/policy/status")
    def policy_status() -> dict[str, Any]:
        return query_service().policy_status()

    @api.get("/policy/audits")
    def policy_audits(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        scope: str | None = None,
        policy_key: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        return query_service().policy_audit_history(
            limit=limit,
            offset=offset,
            scope=scope,
            policy_key=policy_key,
            since=since,
        )

    @api.get("/health/issues")
    def health_issues(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return query_service().health_issues(limit=limit)

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

    @api.post("/approvals/{approval_id}/approve")
    def approve_approval(
        approval_id: str,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        return command_service().approve(
            approval_id,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
            command_id=payload.command_id,
        ).as_dict()

    @api.post("/approvals/{approval_id}/reject")
    def reject_approval(
        approval_id: str,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        return command_service().reject(
            approval_id,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
            command_id=payload.command_id,
        ).as_dict()

    @api.post("/tasks/{task_id}/send")
    def send_task(
        task_id: str,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        final_reply = _required_text(payload.final_reply, field="final_reply")
        return command_service().send(
            task_id,
            final_reply,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
            command_id=payload.command_id,
        ).as_dict()

    @api.post("/maintenance/expire-approvals")
    def expire_approvals(body: Annotated[CommandRequest | None, Body()] = None) -> dict[str, Any]:
        payload = body or CommandRequest()
        return command_service().expire_approvals(
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
        ).as_dict()

    @api.post("/dispatch/actions/{action_id}/retry")
    def retry_dispatch_action(
        action_id: int,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        return command_service().retry_dispatch_action(
            action_id,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
        ).as_dict()

    @api.post("/dispatch/actions/{action_id}/cancel")
    def cancel_dispatch_action(
        action_id: int,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        return command_service().cancel_dispatch_action(
            action_id,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
        ).as_dict()

    @api.post("/dispatch/actions/{action_id}/mark-sent")
    def mark_dispatch_sent(
        action_id: int,
        body: Annotated[CommandRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        payload = body or CommandRequest()
        sent_message_id = _required_text(payload.sent_message_id, field="sent_message_id")
        try:
            service = command_service(needs_readback_marker=True)
        except Exception as exc:
            return _command_error_result(
                command="dispatch.mark_sent",
                actor=LOCAL_CONSOLE_ACTOR,
                reason=payload.reason,
                target={"type": "dispatch_action", "action_id": action_id},
                error=f"dispatch mark-sent readback marker unavailable: {exc}",
            )
        return service.mark_dispatch_sent(
            action_id,
            sent_message_id=sent_message_id,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
        ).as_dict()

    @api.post("/policy/import-config")
    def import_policy_config(body: Annotated[PolicyImportRequest | None, Body()] = None) -> dict[str, Any]:
        payload = body or PolicyImportRequest()
        return command_service().import_policy_config(
            loaded_config.config,
            replace=payload.replace,
            used_defaults=loaded_config.reply_policy_used_defaults,
            actor=LOCAL_CONSOLE_ACTOR,
            reason=payload.reason,
        ).as_dict()

    @api.patch("/policy/global")
    def update_global_policy(body: GlobalPolicyUpdateRequest) -> dict[str, Any]:
        return command_service().update_global_policy(
            body.changes(),
            actor=LOCAL_CONSOLE_ACTOR,
            reason=body.reason,
        ).as_dict()

    @api.patch("/policy/chats/{chat_id}")
    def update_chat_policy(chat_id: str, body: ChatPolicyUpdateRequest) -> dict[str, Any]:
        return command_service().update_chat_policy(
            chat_id,
            body.changes(),
            actor=LOCAL_CONSOLE_ACTOR,
            reason=body.reason,
        ).as_dict()

    @api.api_route("", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def api_not_found(path: str = "") -> None:
        _raise_api_error(404, "not_found", f"API route not found: /api/{path}".rstrip("/"))

    app.include_router(api)
    _install_static_routes(app, resolved_static_dir)
    return app


def _build_dispatch_readback_marker(
    *,
    loaded_config: LoadedConfig,
    store: SQLiteStore,
) -> Dispatcher:
    config = loaded_config.config
    jsonl_path = resolve_relative_path(config.logging.jsonl_path, loaded_config.base_dir)
    text_path = (
        None
        if config.logging.text_path is None
        else resolve_relative_path(config.logging.text_path, loaded_config.base_dir)
    )
    client = LarkCliClient(
        path=config.lark_cli.path,
        timeout_seconds=config.lark_cli.timeout_seconds,
        cwd=loaded_config.base_dir,
    )
    return Dispatcher(
        store=store,
        feishu_client=client,
        config=config,
        logger=JSONLLogger(
            jsonl_path,
            level=config.logging.level,
            console=config.logging.console,
            text_path=text_path,
        ),
    )


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


def _required_text(value: str | None, *, field: str) -> str:
    if value is None or not value.strip():
        _raise_api_error(
            400,
            "validation_failed",
            f"{field} is required.",
            details={"field": field},
        )
    return value


def _policy_request_changes(body: BaseModel, fields: tuple[str, ...]) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    return {field: data[field] for field in fields if field in data}


def _command_error_result(
    *,
    command: str,
    actor: str,
    reason: str | None,
    target: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "command": command,
        "actor": actor,
        "reason": reason,
        "target": target,
        "changed": False,
        "result": {"error": error},
        "warnings": [],
        "next_actions": [],
    }


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
