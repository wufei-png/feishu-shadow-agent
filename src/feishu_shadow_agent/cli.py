from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, TypedDict

import yaml

from .agent_backend_factory import coworker_runtime_config, create_agent_backend
from .agent_invocation import AgentInvoker
from .card_actions import create_card_action_connection
from .codex_app_server import CodexAppServer
from .config import ConfigError, ConfigService, CoworkerConfig, LoadedConfig
from .console_api import (
    console_static_ready,
    create_console_app,
    default_console_static_dir,
)
from .console_security import (
    console_access_url,
    generate_console_token,
    validate_console_bind_host,
)
from .coworker import CoworkerService, CoworkerWorker
from .coworker_native import NativeCoworkerService, NativeCoworkerWorker
from .coworker_native_state import NativeCoworkerState
from .coworker_native_store import NativeDeliveryStore
from .coworker_replay import (
    CoworkerReplaySnapshot,
    replay_recorded_events,
    snapshot_from_local_store,
)
from .coworker_store import CoworkerStore
from .daemon import Daemon
from .dispatcher import Dispatcher
from .evals import EvalError
from .evals.config import load_evaluation_config
from .evals.service import EvalService
from .feishu.lark_cli import LarkCliClient, LarkCliCommandError
from .health import HealthSuite, has_critical_failure, summarize_results
from .jsonl import JSONLLogger
from .operator_commands import CommandResult, OperatorCommandService, command_exit_code
from .operator_query import OperatorQueryService
from .paths import resolve_agent_working_dir, resolve_relative_path
from .processing import TaskProcessingService
from .replay import replay_message_dry_run
from .reply_style import ReplyStyleRefresher
from .retention import RetentionService
from .runtime_mode import (
    dry_run_config,
    initialize_dry_run_policy,
    sync_production_policy,
)
from .store.policy_store import COWORKER_POLICY_KEY
from .store.schema import IncompatibleSchemaError
from .store.sqlite_store import SQLiteStore
from .time_utils import shift_instant
from .types import new_run_id, utc_now_iso


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    mode = getattr(args, "mode", None)
    if mode == "dry-run" and args.command in {"config", "eval"}:
        print("error: --mode dry-run is not supported by this command", file=sys.stderr)
        return 2
    if mode == "dry-run" and args.command in {"approve", "reject", "send"}:
        print(
            "error: dry-run approvals are handled in the local Console", file=sys.stderr
        )
        return 2
    if mode == "dry-run" and args.command == "doctor" and args.send_test:
        print("error: dry-run doctor cannot send a test message", file=sys.stderr)
        return 2
    if (
        mode == "dry-run"
        and args.command == "dispatch"
        and args.dispatch_command == "mark-sent"
    ):
        print(
            "error: dry-run actions cannot be marked as actually sent", file=sys.stderr
        )
        return 2
    if mode == "dry-run" and args.command == "reply-style" and not args.dry_run:
        print("error: dry-run mode supports reply-style preview only", file=sys.stderr)
        return 2
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except EvalError as exc:
        print(f"eval error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu_shadow_agent")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="run health checks")
    _add_config_arg(doctor)
    doctor.add_argument(
        "--send-test", action="store_true", help="send a real test DM to owner"
    )
    doctor.set_defaults(handler=_handle_doctor)

    daemon = subparsers.add_parser(
        "daemon", help="run the daemon", description="run the daemon"
    )
    _add_config_arg(daemon)
    daemon.add_argument(
        "--dry-run",
        action="store_true",
        help="do not send external replies; record local state and dispatch previews",
    )
    daemon.set_defaults(handler=_handle_daemon)

    console = subparsers.add_parser("console", help="run the local Operator Console")
    _add_config_arg(console)
    console.add_argument("--host", default="127.0.0.1", help="loopback host to bind")
    console.add_argument("--port", type=int, default=8765, help="local port to bind")
    console.set_defaults(handler=_handle_console)

    config = subparsers.add_parser("config", help="configuration helpers")
    config_subparsers = config.add_subparsers(dest="config_command")
    config_show = config_subparsers.add_parser("show", help="print config")
    _add_config_arg(config_show)
    config_show.add_argument(
        "--redacted", action="store_true", help="redact secret-like fields"
    )
    config_show.set_defaults(handler=_handle_config_show)
    config_schema = config_subparsers.add_parser(
        "schema", help="print config JSON schema"
    )
    config_schema.set_defaults(handler=_handle_config_schema)
    config_validate = config_subparsers.add_parser(
        "validate", help="validate config without runtime health checks"
    )
    _add_config_arg(config_validate)
    config_validate.set_defaults(handler=_handle_config_validate)

    status = subparsers.add_parser("status", help="show daemon and queue status")
    _add_config_arg(status)
    status.set_defaults(handler=_handle_status)

    coworker = subparsers.add_parser("coworker", help="AI coworker status and recovery")
    coworker_subparsers = coworker.add_subparsers(dest="coworker_command")
    coworker_run = coworker_subparsers.add_parser(
        "run", help="start the selected coworker runtime"
    )
    _add_config_arg(coworker_run)
    coworker_run.add_argument("--runtime", choices=("codex", "hermes"), required=True)
    coworker_run.add_argument("--reply-mode", choices=("quote", "topic", "auto"))
    coworker_run.add_argument("--dry-run", action="store_true")
    coworker_run.set_defaults(handler=_handle_coworker_run)
    coworker_status = coworker_subparsers.add_parser(
        "status", help="show coworker sessions and pending work"
    )
    _add_config_arg(coworker_status)
    coworker_status.set_defaults(handler=_handle_coworker_status)
    coworker_resolve = coworker_subparsers.add_parser(
        "resolve-delivery",
        help="record a checked receipt or discard an uncertain delivery",
    )
    _add_config_arg(coworker_resolve)
    coworker_resolve.add_argument("--delivery-id", required=True, type=int)
    resolution = coworker_resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--sent-message-id")
    resolution.add_argument("--discard", action="store_true")
    coworker_resolve.add_argument("--thread-id")
    coworker_resolve.add_argument(
        "--daemon-stopped", action="store_true", required=True
    )
    coworker_resolve.set_defaults(handler=_handle_coworker_resolve_delivery)
    for command, resume in (("pause", False), ("resume", True)):
        handoff = coworker_subparsers.add_parser(
            command, help="pause or return one Codex session"
        )
        _add_config_arg(handoff)
        handoff.add_argument("--session-id", required=True, type=int)
        if not resume:
            handoff.add_argument("--retry", action="store_true")
        handoff.set_defaults(handler=_handle_coworker_handoff, handoff_resume=resume)
    coworker_discard = coworker_subparsers.add_parser(
        "discard-event", help="discard an uncertain event without replay"
    )
    _add_config_arg(coworker_discard)
    coworker_discard.add_argument("--event-id", required=True, type=int)
    coworker_discard.add_argument(
        "--daemon-stopped", action="store_true", required=True
    )
    coworker_discard.set_defaults(handler=_handle_coworker_discard)
    coworker_reset = coworker_subparsers.add_parser(
        "reset-session", help="close a topic session so its next turn creates a new one"
    )
    _add_config_arg(coworker_reset)
    coworker_reset.add_argument("--session-id", required=True, type=int)
    coworker_reset.add_argument("--daemon-stopped", action="store_true", required=True)
    coworker_reset.set_defaults(handler=_handle_coworker_reset)
    coworker_memory = coworker_subparsers.add_parser(
        "reset-memory-review", help="clear a reviewed memory maintenance failure"
    )
    _add_config_arg(coworker_memory)
    coworker_memory.add_argument("--daemon-stopped", action="store_true", required=True)
    coworker_memory.set_defaults(handler=_handle_coworker_memory_reset)
    coworker_replay = coworker_subparsers.add_parser(
        "replay", help="replay recorded SDK events offline with a fake backend"
    )
    coworker_replay.add_argument("--snapshot", required=True)
    coworker_replay.add_argument("--trigger-mode", choices=["mention", "followups"])
    coworker_replay.add_argument("--chat-layout", choices=["group", "topic"])
    coworker_replay.add_argument(
        "--history-source", choices=["live_on_demand", "local_cache"]
    )
    coworker_replay.set_defaults(handler=_handle_coworker_replay)
    coworker_export = coworker_subparsers.add_parser(
        "export-replay",
        help="reconstruct a private replay snapshot from stored messages",
    )
    _add_config_arg(coworker_export)
    coworker_export.add_argument("--app-id", required=True)
    coworker_export.add_argument("--bot-open-id", required=True)
    coworker_export.add_argument("--output", required=True)
    coworker_export.add_argument("--limit", type=int, default=100)
    coworker_export.set_defaults(handler=_handle_coworker_export)

    replay = subparsers.add_parser(
        "replay", help="explain local state and preview dispatch"
    )
    _add_config_arg(replay)
    replay.add_argument("--message-id", required=True)
    replay.add_argument("--dry-run", action="store_true", required=True)
    replay.set_defaults(handler=_handle_replay)

    eval_parser = subparsers.add_parser("eval", help="evaluation helpers")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command")
    eval_capture = eval_subparsers.add_parser("capture", help="capture eval cases")
    _add_config_arg(eval_capture)
    eval_capture.add_argument("--lookback-days", type=int, default=2)
    eval_capture.add_argument("--limit", type=int, default=20)
    eval_capture.add_argument("--message-id")
    eval_capture.add_argument("--context-before", type=int, default=20)
    eval_capture.add_argument("--context-after", type=int, default=0)
    eval_capture.add_argument("--label")
    eval_capture.add_argument("--allow-sensitive-config", action="store_true")
    eval_capture.set_defaults(handler=_handle_eval_capture)

    eval_ingress = eval_subparsers.add_parser(
        "run-ingress", help="evaluate group ingress filtering"
    )
    _add_config_arg(eval_ingress)
    eval_ingress.add_argument("--chat-id")
    eval_ingress.add_argument("--snapshot", type=Path)
    eval_ingress.add_argument("--start")
    eval_ingress.add_argument("--end")
    eval_ingress.add_argument("--lookback-days", type=int)
    eval_ingress.add_argument("--golden", type=Path)
    eval_ingress.add_argument("--label")
    eval_ingress.add_argument("--dry-run-backend", action="store_true")
    eval_ingress.add_argument("--allow-sensitive-config", action="store_true")
    eval_ingress.set_defaults(handler=_handle_eval_run_ingress)

    eval_router = eval_subparsers.add_parser("run-router", help="run router eval")
    _add_config_arg(eval_router)
    eval_router.add_argument("--case", type=Path)
    eval_router.add_argument("--cases", type=Path)
    eval_router.add_argument("--label")
    eval_router.add_argument("--repeat", type=int, default=1)
    eval_router.add_argument("--dry-run-backend", action="store_true")
    eval_router.add_argument("--allow-sensitive-config", action="store_true")
    eval_router.set_defaults(handler=_handle_eval_run_router)

    eval_task_session = eval_subparsers.add_parser(
        "run-task-session", help="run task session eval"
    )
    _add_config_arg(eval_task_session)
    eval_task_session.add_argument("--case", type=Path)
    eval_task_session.add_argument("--cases", type=Path)
    eval_task_session.add_argument("--label")
    eval_task_session.add_argument("--repeat", type=int, default=1)
    eval_task_session.add_argument("--dry-run-backend", action="store_true")
    eval_task_session.add_argument("--allow-sensitive-config", action="store_true")
    eval_task_session.set_defaults(handler=_handle_eval_run_task_session)

    eval_full_chain = eval_subparsers.add_parser(
        "run-full-chain", help="run full chain eval"
    )
    _add_config_arg(eval_full_chain)
    eval_full_chain.add_argument("--case", type=Path)
    eval_full_chain.add_argument("--cases", type=Path)
    eval_full_chain.add_argument("--label")
    eval_full_chain.add_argument("--repeat", type=int, default=1)
    eval_full_chain.add_argument("--dry-run-backend", action="store_true")
    eval_full_chain.add_argument("--allow-sensitive-config", action="store_true")
    eval_full_chain.set_defaults(handler=_handle_eval_run_full_chain)

    eval_promote = eval_subparsers.add_parser(
        "promote", help="promote reviewed eval labels to golden"
    )
    _add_config_arg(eval_promote)
    eval_promote.add_argument(
        "--type",
        dest="eval_type",
        required=True,
        choices=["ingress", "router", "task-session", "full-chain"],
    )
    eval_promote.add_argument("--run", type=Path)
    eval_promote.add_argument("--case", type=Path)
    eval_promote.add_argument("--review", type=Path, required=True)
    eval_promote.add_argument("--name", required=True)
    eval_promote.add_argument("--allow-sensitive-config", action="store_true")
    eval_promote.set_defaults(handler=_handle_eval_promote)

    retention = subparsers.add_parser("retention", help="retention helpers")
    retention_subparsers = retention.add_subparsers(dest="retention_command")
    retention_prune = retention_subparsers.add_parser(
        "prune", help="prune expired local data"
    )
    _add_config_arg(retention_prune)
    retention_prune.add_argument(
        "--dry-run", action="store_true", help="preview retention cleanup"
    )
    retention_prune.set_defaults(handler=_handle_retention_prune)

    reply_style = subparsers.add_parser(
        "reply-style", help="reply style profile helpers"
    )
    reply_style_subparsers = reply_style.add_subparsers(dest="reply_style_command")
    reply_style_refresh = reply_style_subparsers.add_parser(
        "refresh", help="refresh owner reply style profile"
    )
    _add_config_arg(reply_style_refresh)
    reply_style_refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="pull and filter samples without calling Hermes or writing",
    )
    reply_style_refresh.set_defaults(handler=_handle_reply_style_refresh)

    maintenance = subparsers.add_parser(
        "maintenance", help="explicit maintenance helpers"
    )
    maintenance_subparsers = maintenance.add_subparsers(dest="maintenance_command")
    maintenance_expire_approvals = maintenance_subparsers.add_parser(
        "expire-approvals",
        help="expire overdue pending approvals",
    )
    _add_config_arg(maintenance_expire_approvals)
    maintenance_expire_approvals.set_defaults(
        handler=_handle_maintenance_expire_approvals
    )

    policy = subparsers.add_parser("policy", help="product policy helpers")
    policy_subparsers = policy.add_subparsers(dest="policy_command")
    policy_import_config = policy_subparsers.add_parser(
        "import-config",
        help="import config.yaml policy fields into Product Policy Store",
    )
    _add_config_arg(policy_import_config)
    policy_import_config.add_argument(
        "--replace",
        action="store_true",
        help="replace global policy and config-listed chat policies instead of only filling missing rows",
    )
    policy_import_config.add_argument("--reason", help="optional policy audit reason")
    policy_import_config.set_defaults(handler=_handle_policy_import_config)
    policy_sync = policy_subparsers.add_parser(
        "sync-production", help="copy current production policy into dry-run"
    )
    _add_config_arg(policy_sync)
    policy_sync.set_defaults(handler=_handle_policy_sync_production)
    policy_update_global = policy_subparsers.add_parser(
        "update-global",
        help="update Product Policy Store global policy fields",
    )
    _add_config_arg(policy_update_global)
    policy_update_global.add_argument(
        "--p2p-auto-reply", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_global.add_argument(
        "--unknown-group-auto-reply", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_global.add_argument(
        "--bot-joined", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_global.add_argument(
        "--reply-identity", choices=["bot_preferred", "bot", "user"]
    )
    policy_update_global.add_argument(
        "--allow-user-fallback", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_global.add_argument(
        "--resource-download", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_global.add_argument("--reason", help="optional policy audit reason")
    policy_update_global.set_defaults(handler=_handle_policy_update_global)
    policy_update_chat = policy_subparsers.add_parser(
        "update-chat",
        help="update a Product Policy Store chat policy",
    )
    _add_config_arg(policy_update_chat)
    policy_update_chat.add_argument("--chat-id", required=True)
    policy_update_chat.add_argument("--name")
    policy_update_chat.add_argument(
        "--auto-reply", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_chat.add_argument(
        "--bot-joined", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_chat.add_argument(
        "--reply-identity", choices=["bot_preferred", "bot", "user"]
    )
    policy_update_chat.add_argument(
        "--allow-user-fallback", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_chat.add_argument(
        "--resource-download", type=_parse_bool_arg, metavar="true|false"
    )
    policy_update_chat.add_argument("--reason", help="optional policy audit reason")
    policy_update_chat.set_defaults(handler=_handle_policy_update_chat)
    policy_delete_chat = policy_subparsers.add_parser(
        "delete-chat",
        help="delete a Product Policy Store chat policy override",
    )
    _add_config_arg(policy_delete_chat)
    policy_delete_chat.add_argument("--chat-id", required=True)
    policy_delete_chat.add_argument("--reason", help="optional policy audit reason")
    policy_delete_chat.set_defaults(handler=_handle_policy_delete_chat)

    dispatch = subparsers.add_parser("dispatch", help="dispatch recovery helpers")
    dispatch_subparsers = dispatch.add_subparsers(dest="dispatch_command")
    dispatch_inspect = dispatch_subparsers.add_parser(
        "inspect", help="inspect an action and dispatch attempts"
    )
    _add_config_arg(dispatch_inspect)
    dispatch_inspect.add_argument("--action-id", type=int, required=True)
    dispatch_inspect.set_defaults(handler=_handle_dispatch_inspect)
    dispatch_mark_sent = dispatch_subparsers.add_parser(
        "mark-sent", help="verify readback and mark an action sent"
    )
    _add_config_arg(dispatch_mark_sent)
    dispatch_mark_sent.add_argument("--action-id", type=int, required=True)
    dispatch_mark_sent.add_argument("--sent-message-id", required=True)
    dispatch_mark_sent.set_defaults(handler=_handle_dispatch_mark_sent)
    dispatch_retry = dispatch_subparsers.add_parser(
        "retry", help="requeue a failed dispatch action"
    )
    _add_config_arg(dispatch_retry)
    dispatch_retry.add_argument("--action-id", type=int, required=True)
    dispatch_retry.set_defaults(handler=_handle_dispatch_retry)
    dispatch_cancel = dispatch_subparsers.add_parser(
        "cancel", help="cancel a dispatch action"
    )
    _add_config_arg(dispatch_cancel)
    dispatch_cancel.add_argument("--action-id", type=int, required=True)
    dispatch_cancel.set_defaults(handler=_handle_dispatch_cancel)

    approve = subparsers.add_parser("approve", help="approve a pending approval")
    _add_config_arg(approve)
    approve.add_argument("approval_id")
    approve.set_defaults(handler=_handle_approve)

    reject = subparsers.add_parser("reject", help="reject a pending approval")
    _add_config_arg(reject)
    reject.add_argument("approval_id")
    reject.set_defaults(handler=_handle_reject)

    send = subparsers.add_parser("send", help="send a final reply for a task")
    _add_config_arg(send)
    send.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="read final reply from stdin",
    )
    send.add_argument("--location", choices=("main", "thread"))
    send.add_argument("task_id")
    send.add_argument("text", nargs=argparse.REMAINDER)
    send.set_defaults(handler=_handle_send)

    task = subparsers.add_parser("task", help="task lifecycle helpers")
    task_subparsers = task.add_subparsers(dest="task_command")
    task_close = task_subparsers.add_parser(
        "close", help="close a task without deleting history"
    )
    _add_config_arg(task_close)
    task_close.add_argument("--task-id", required=True)
    task_close.add_argument("--reason", help="optional operator audit reason")
    task_close.set_defaults(handler=_handle_task_close)
    task_reopen = task_subparsers.add_parser("reopen", help="reopen a closed task")
    _add_config_arg(task_reopen)
    task_reopen.add_argument("--task-id", required=True)
    task_reopen.add_argument("--reason", help="optional operator audit reason")
    task_reopen.set_defaults(handler=_handle_task_reopen)
    task_retry = task_subparsers.add_parser(
        "retry-processing", help="queue a terminal or blocked processing stage"
    )
    _add_config_arg(task_retry)
    task_retry.add_argument("--message-id", required=True)
    task_retry.add_argument(
        "--stage",
        required=True,
        choices=["task_router", "resource_download", "task_session"],
    )
    task_retry.add_argument("--reason", help="optional operator audit reason")
    task_retry.set_defaults(handler=_handle_task_retry_processing)
    task_background = task_subparsers.add_parser(
        "set-background", help="set owner-provided task background"
    )
    _add_config_arg(task_background)
    task_background.add_argument("--task-id", required=True)
    task_background.add_argument("--reason", help="optional operator audit reason")
    task_background.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="read task background from stdin",
    )
    task_background.add_argument("text", nargs=argparse.REMAINDER)
    task_background.set_defaults(handler=_handle_task_set_background)
    task_clear_background = task_subparsers.add_parser(
        "clear-background", help="clear owner-provided task background"
    )
    _add_config_arg(task_clear_background)
    task_clear_background.add_argument("--task-id", required=True)
    task_clear_background.add_argument(
        "--reason", help="optional operator audit reason"
    )
    task_clear_background.set_defaults(handler=_handle_task_clear_background)

    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument(
        "--mode",
        choices=("production", "dry-run"),
        help="select production or isolated dry-run state",
    )


def _handle_doctor(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config, mode=args.mode)
    run_id = new_run_id("doctor")
    store.record_run_start(
        run_id=run_id, dry_run=not args.send_test, **_git_info(Path.cwd())
    )
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        run_id=run_id,
        dry_run_mode=args.mode == "dry-run",
    )
    results = suite.run(send_test=args.send_test)
    summary = summarize_results(results)
    status = "health_failed" if has_critical_failure(results) else "ok"
    store.record_run_finish(run_id=run_id, status=status, health_summary=summary)
    for result in results:
        marker = "OK" if result.status == "ok" else result.status.upper()
        print(f"[{marker}] {result.name}: {result.message}")
    logger.emit("info", "doctor_completed", run_id=run_id, data=summary)
    return 2 if has_critical_failure(results) else 0


def _handle_daemon(args: argparse.Namespace) -> int:
    if args.dry_run and args.mode == "production":
        print(
            "daemon error: --dry-run conflicts with --mode production", file=sys.stderr
        )
        return 2
    args.dry_run = args.dry_run or args.mode == "dry-run"
    loaded, store, logger = _load_runtime(
        args.config, mode="dry-run" if args.dry_run else args.mode
    )
    run_id = new_run_id("run")
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    suite = HealthSuite(
        loaded_config=loaded,
        store=store,
        feishu_client=client,
        run_id=run_id,
        dry_run_mode=args.dry_run,
    )
    raw_coworker = store.get_product_policy(COWORKER_POLICY_KEY)
    if raw_coworker is not None and "send_mode" not in raw_coworker:
        raw_coworker = {**raw_coworker, "send_mode": "project"}
    coworker_policy = (
        None if raw_coworker is None else CoworkerConfig.model_validate(raw_coworker)
    )
    coworker_enabled = coworker_policy is not None and coworker_policy.enabled
    native = getattr(args, "runtime", None) == "codex"
    if getattr(args, "runtime", None) == "codex" and (
        not coworker_enabled
        or coworker_policy is None
        or coworker_policy.provider != "codex"
    ):
        print(
            "coworker error: the active policy must enable coworker with provider codex",
            file=sys.stderr,
        )
        return 2
    backend_config = loaded.config.agent_backend
    agent_working_dir = resolve_agent_working_dir(
        backend_config.working_dir, loaded.base_dir
    )
    task_processor = None
    if not coworker_enabled:
        agent_backend = create_agent_backend(loaded.config, base_dir=loaded.base_dir)
        task_processor = TaskProcessingService(
            store=store,
            config=loaded.config,
            agent_backend=agent_backend,
            logger=logger,
            agent_working_dir=agent_working_dir,
            config_base_dir=loaded.base_dir,
            dry_run=args.dry_run,
            approval_scope="shadow",
        )
    daemon = Daemon(
        store=store,
        logger=logger,
        health_suite=suite,
        tick_interval_seconds=loaded.config.daemon.tick_interval_seconds,
        dry_run=args.dry_run,
        app_config=loaded.config,
        feishu_client=client,
        task_processor=task_processor,
        run_metadata=dict(_git_info(Path.cwd())),
        config_base_dir=loaded.base_dir,
        business_mode="coworker" if coworker_enabled else "shadow",
        coworker_send_mode=coworker_policy.send_mode
        if coworker_enabled and coworker_policy
        else None,
        coworker_chat_id=coworker_policy.chat_id
        if coworker_enabled and coworker_policy
        else None,
        native_coworker=native,
    )
    message_handler = None
    on_message_reconnect = None
    if coworker_enabled:
        if coworker_policy is None or coworker_policy.chat_id is None:
            raise ValueError("enabled coworker policy requires a chat ID")
        app_id = os.environ.get(loaded.config.interactive_cards.app_id_env, "")
        if CoworkerStore(store).runtime_conflicts(
            app_id=app_id, chat_id=coworker_policy.chat_id, native=native
        ):
            print(
                "coworker error: the other runtime has unfinished work; resolve or drain it before switching",
                file=sys.stderr,
            )
            return 2
        try:
            chat_layout = client.get_chat_layout(chat_id=coworker_policy.chat_id)
        except (LarkCliCommandError, ValueError) as exc:
            print(
                f"coworker error: cannot determine group layout ({type(exc).__name__}); "
                "check bot membership and group information permissions",
                file=sys.stderr,
            )
            return 2
        service_class = NativeCoworkerService if native else CoworkerService
        service_options: dict[str, Any] = {}
        if native:
            service_options["reply_mode"] = getattr(args, "reply_mode", None) or "quote"
        service = service_class(
            store=store,
            config=loaded.config,
            feishu=client,
            logger=logger,
            base_dir=loaded.base_dir,
            app_id=app_id,
            dry_run=args.dry_run,
            chat_layout=chat_layout,
            **service_options,
        )
        if isinstance(service, NativeCoworkerService):
            try:
                service.validate_workspace(agent_working_dir)
            except ValueError as exc:
                print(f"coworker error: {exc}", file=sys.stderr)
                return 2
        if coworker_policy.provider == "codex":
            runtime = coworker_runtime_config(loaded.config, coworker_policy)
            service.app_server = CodexAppServer(
                config=runtime.agent_backend.codex,
                skill_names=runtime.agent_backend.codex.skills,
                explicit_context_paths=runtime.agent_backend.explicit_context.paths,
                on_notification=service.on_app_server_notification,
                confine_production=native,
                writable_roots=[service.memory_root]
                if isinstance(service, NativeCoworkerService) and service.memory_root
                else None,
                on_read_topic=service.read_topic
                if isinstance(service, NativeCoworkerService)
                else None,
                on_read_group=service.read_group_history
                if isinstance(service, NativeCoworkerService)
                else None,
            )
        worker_class = NativeCoworkerWorker if native else CoworkerWorker
        daemon.coworker_worker = worker_class(
            service,
            runtime_allows_processing=daemon.runtime_health_allows_card_actions,
            wake_daemon=daemon.wake,
        )
        message_handler = service.receive
        on_message_reconnect = partial(service.events.mark_history_gaps, service.app_id)
    if (
        loaded.config.interactive_cards.enabled and not args.dry_run
    ) or coworker_enabled:
        daemon.card_action_connection = create_card_action_connection(
            store=store,
            config=loaded.config,
            logger=logger,
            wake=daemon.wake,
            execution_mode="dry_run" if args.dry_run else "production",
            runtime_allows_actions=daemon.runtime_health_allows_card_actions,
            message_handler=message_handler,
            register_card_actions=loaded.config.interactive_cards.enabled
            and not args.dry_run
            and (
                not coworker_enabled
                or (
                    coworker_policy is not None
                    and (native or coworker_policy.send_mode == "project")
                )
            ),
            on_message_reconnect=on_message_reconnect,
            approval_scope="coworker" if coworker_enabled else "shadow",
        )
    return daemon.run_forever()


def _handle_coworker_run(args: argparse.Namespace) -> int:
    if args.runtime == "codex":
        return _handle_daemon(args)
    if args.dry_run or args.mode == "dry-run":
        print("coworker error: Hermes Gateway has no dry-run mode", file=sys.stderr)
        return 2
    if (
        args.config is not None
        or args.mode is not None
        or getattr(args, "reply_mode", None) is not None
    ):
        print("coworker error: Hermes uses its own coworker profile", file=sys.stderr)
        return 2
    try:
        return subprocess.run(
            ["hermes", "-p", "coworker", "gateway", "start"],  # noqa: S607
            check=False,
        ).returncode
    except FileNotFoundError:
        print("coworker error: hermes executable not found", file=sys.stderr)
        return 2


def _handle_console(args: argparse.Namespace) -> int:
    try:
        host = validate_console_bind_host(args.host)
    except ValueError as exc:
        print(f"console error: {exc}", file=sys.stderr)
        return 2
    if args.port <= 0 or args.port > 65535:
        print("console error: port must be between 1 and 65535", file=sys.stderr)
        return 2
    static_dir = default_console_static_dir()
    if not console_static_ready(static_dir):
        print(
            "console error: renderer assets are missing. "
            "Run `npm --prefix frontend/operator-console run build` first.",
            file=sys.stderr,
        )
        return 2
    loaded, store, _ = _load_runtime(args.config, mode=args.mode)
    store.initialize()
    token = generate_console_token()
    app = create_console_app(
        loaded_config=loaded,
        store=store,
        token=token,
        host=host,
        port=args.port,
        static_dir=static_dir,
        execution_mode="dry_run" if args.mode == "dry-run" else "production",
    )
    print(
        f"Operator Console ({args.mode or 'production'}): {console_access_url(host=host, port=args.port, token=token)}",
        flush=True,
    )
    _run_console_server(app, host=host, port=args.port)
    return 0


def _handle_config_show(args: argparse.Namespace) -> int:
    service = ConfigService()
    loaded = service.load(args.config)
    data = (
        service.redacted_dict(loaded.config)
        if args.redacted
        else loaded.config.model_dump(mode="json")
    )
    print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_config_schema(args: argparse.Namespace) -> int:
    schema = ConfigService().json_schema_dict()
    print(json.dumps(schema, ensure_ascii=False, indent=2), end="\n")
    return 0


def _handle_config_validate(args: argparse.Namespace) -> int:
    loaded = ConfigService().load(args.config)
    print(f"config ok: {loaded.path}")
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    loaded = ConfigService().load(args.config)
    if args.mode == "dry-run":
        loaded, store, _ = _load_runtime(args.config, mode=args.mode)
    else:
        store = SQLiteStore(
            resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
        )
    sqlite_path = resolve_relative_path(
        loaded.config.storage.sqlite_path, loaded.base_dir
    )
    if sqlite_path.exists():
        store.initialize()
    snapshot = OperatorQueryService(
        store, policy_import_source=loaded.config, base_dir=loaded.base_dir
    ).dashboard_snapshot()
    snapshot["runtime_mode"] = args.mode or "production"
    print(yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_coworker_status(args: argparse.Namespace) -> int:
    loaded = ConfigService().load(args.config)
    if args.mode == "dry-run":
        loaded = dry_run_config(loaded)
    path = resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
    if not path.exists():
        print(
            yaml.safe_dump({"status": "database_missing"}, allow_unicode=True), end=""
        )
        return 0
    store = SQLiteStore(path)
    snapshot = CoworkerStore(store).status()
    effective_policy = store.get_product_policy(COWORKER_POLICY_KEY)
    snapshot["effective_policy"] = effective_policy
    if effective_policy is not None:
        snapshot["effective_send_mode"] = effective_policy.get("send_mode", "project")
    snapshot["policy_matches_config"] = (
        effective_policy == loaded.config.coworker.model_dump(mode="json")
        if effective_policy is not None
        else not loaded.config.coworker.enabled
    )
    print(yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_coworker_discard(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    try:
        CoworkerStore(store).discard_event(
            args.event_id, daemon_stopped=args.daemon_stopped
        )
    except ValueError as exc:
        print(f"coworker error: {exc}", file=sys.stderr)
        return 2
    print(f"discarded event {args.event_id}")
    return 0


def _handle_coworker_resolve_delivery(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    try:
        NativeDeliveryStore(store).resolve_uncertain(
            args.delivery_id,
            sent_message_id=args.sent_message_id,
            daemon_stopped=args.daemon_stopped,
            thread_id=args.thread_id,
        )
    except ValueError as exc:
        print(f"coworker error: {exc}", file=sys.stderr)
        return 2
    print(f"delivery {args.delivery_id} resolved")
    return 0


def _handle_coworker_handoff(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    try:
        NativeCoworkerState(store).request_handoff(
            args.session_id,
            resume=args.handoff_resume,
            retry=getattr(args, "retry", False),
        )
    except ValueError as exc:
        print(f"coworker error: {exc}", file=sys.stderr)
        return 2
    print("handoff requested; use coworker status to check progress")
    return 0


def _handle_coworker_reset(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    try:
        CoworkerStore(store).reset_session(
            args.session_id, daemon_stopped=args.daemon_stopped
        )
    except ValueError as exc:
        print(f"coworker error: {exc}", file=sys.stderr)
        return 2
    print(f"closed session {args.session_id}; the next turn creates a new session")
    return 0


def _handle_coworker_memory_reset(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    checkpoint = store.get_checkpoint("coworker.memory_maintenance") or {}
    if checkpoint.get("state") not in {"needs_review", "in_progress", "failed"}:
        print(
            "coworker error: memory maintenance is not awaiting review", file=sys.stderr
        )
        return 2
    store.set_checkpoint(
        "coworker.memory_maintenance",
        checkpoint | {"state": "idle", "error_type": None},
    )
    print("memory maintenance review cleared")
    return 0


def _handle_coworker_replay(args: argparse.Namespace) -> int:
    try:
        path = Path(args.snapshot)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("snapshot is too large")
        snapshot = CoworkerReplaySnapshot.model_validate_json(path.read_bytes())
        changes: dict[str, Any] = {}
        if args.chat_layout:
            changes["chat_layout"] = args.chat_layout
        if args.trigger_mode:
            changes["trigger_mode"] = args.trigger_mode
        if args.history_source:
            changes["history_source"] = args.history_source
        snapshot = snapshot.model_copy(update=changes)
        report = replay_recorded_events(snapshot)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Validation errors may contain message bodies; do not print them.
        print(f"coworker replay failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _handle_coworker_export(args: argparse.Namespace) -> int:
    try:
        loaded = ConfigService().load(args.config)
        if args.mode == "dry-run":
            loaded = dry_run_config(loaded)
        database = resolve_relative_path(
            loaded.config.storage.sqlite_path, loaded.base_dir
        )
        snapshot = snapshot_from_local_store(
            database, app_id=args.app_id, bot_open_id=args.bot_open_id, limit=args.limit
        )
        data = snapshot.model_dump_json(indent=2).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(args.output, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
        except Exception:
            Path(args.output).unlink(missing_ok=True)
            raise
    except (
        OSError,
        ValueError,
        TypeError,
        sqlite3.Error,
        IncompatibleSchemaError,
    ) as exc:
        print(f"coworker export failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"wrote {len(snapshot.events)} reconstructed events to {args.output}")
    return 0


def _handle_approve(args: argparse.Namespace) -> int:
    return _handle_local_approval_command(
        args.config, verb="approve", target_id=args.approval_id
    )


def _handle_reject(args: argparse.Namespace) -> int:
    return _handle_local_approval_command(
        args.config, verb="reject", target_id=args.approval_id
    )


def _handle_send(args: argparse.Namespace) -> int:
    final_reply = sys.stdin.read() if args.read_stdin else " ".join(args.text).strip()
    if not final_reply.strip():
        print("send requires final reply text", file=sys.stderr)
        return 2
    return _handle_local_approval_command(
        args.config,
        verb="send",
        target_id=args.task_id,
        final_reply=final_reply,
        reply_location=args.location,
    )


def _handle_task_close(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).close_task(
        args.task_id,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_task_reopen(args: argparse.Namespace) -> int:
    loaded, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).reopen_task(
        args.task_id,
        watch_until=_watch_until_from_now(loaded.config.lifecycle.watch_minutes),
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_task_retry_processing(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).retry_processing(
        args.message_id,
        stage=args.stage,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_task_set_background(args: argparse.Namespace) -> int:
    content = sys.stdin.read() if args.read_stdin else " ".join(args.text).strip()
    if not content.strip():
        print("set-background requires background text", file=sys.stderr)
        return 2
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).update_task_background(
        args.task_id,
        content=content,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_task_clear_background(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).update_task_background(
        args.task_id,
        content=None,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_local_approval_command(
    config_path: str | None,
    *,
    verb: str,
    target_id: str,
    final_reply: str | None = None,
    reply_location: str | None = None,
) -> int:
    loaded, store, _ = _load_runtime(config_path)
    service = OperatorCommandService(
        store,
        keep_watching_until_factory=lambda: _watch_until_from_now(
            loaded.config.lifecycle.watch_minutes
        ),
    )
    if verb == "approve":
        result = service.approve(target_id, actor="local_cli")
    elif verb == "reject":
        result = service.reject(target_id, actor="local_cli")
    else:
        result = service.send(
            target_id,
            final_reply or "",
            actor="local_cli",
            reply_location=reply_location,
        )
    return _emit_command_result(result)


def _handle_replay(args: argparse.Namespace) -> int:
    loaded, store, _ = _load_runtime(args.config, mode=args.mode)
    output = replay_message_dry_run(
        loaded_config=loaded,
        store=store,
        message_id=args.message_id,
        lark_client_factory=LarkCliClient,
    )
    if output is None:
        print(f"message not found: {args.message_id}", file=sys.stderr)
        return 2
    print(yaml.safe_dump(output, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_eval_capture(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    service = EvalService(loaded=loaded)
    if not args.message_id:
        rows = service.capture_candidates(
            lookback_days=args.lookback_days,
            limit=args.limit,
        )
        print(
            yaml.safe_dump({"candidates": rows}, allow_unicode=True, sort_keys=False),
            end="",
        )
        return 0
    case_dir = service.capture_case(
        message_id=args.message_id,
        context_before=args.context_before,
        context_after=args.context_after,
        lookback_days=args.lookback_days,
        label=args.label,
        allow_sensitive_config=args.allow_sensitive_config,
    )
    print(
        yaml.safe_dump(
            {"captured_case": str(case_dir)}, allow_unicode=True, sort_keys=False
        ),
        end="",
    )
    return 0


def _handle_eval_run_ingress(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    service = EvalService(loaded=loaded)
    if args.golden is not None:
        if any(
            value is not None
            for value in (
                args.chat_id,
                args.snapshot,
                args.start,
                args.end,
                args.lookback_days,
            )
        ):
            raise EvalError("--golden cannot be combined with live/snapshot options")
        run_dir, exit_code = service.run_ingress_golden(
            case_dir=args.golden,
            label=args.label,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        print(
            yaml.safe_dump(
                {"run_dir": str(run_dir), "report": str(run_dir / "report.yaml")},
                allow_unicode=True,
                sort_keys=False,
            ),
            end="",
        )
        return exit_code
    run_dir = service.run_ingress(
        chat_id=args.chat_id,
        snapshot=args.snapshot,
        start=args.start,
        end=args.end,
        lookback_days=args.lookback_days,
        label=args.label,
        dry_run_backend=args.dry_run_backend,
        allow_sensitive_config=args.allow_sensitive_config,
    )
    print(
        yaml.safe_dump(
            {
                "run_dir": str(run_dir),
                "review": str(run_dir / "REVIEW.md"),
                "labels": str(run_dir / "labels.review.yaml"),
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        end="",
    )
    return 0


def _handle_eval_run_router(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    _validate_case_args(args.case, args.cases)
    _warn_eval_full_access_repeat(loaded, args.repeat)
    service = EvalService(loaded=loaded)
    if args.cases is not None:
        run_dir, exit_code = service.run_router_cases(
            cases_dir=args.cases,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "summary": str(run_dir / "summary.yaml")}
    else:
        run_dir, exit_code = service.run_router(
            case_dir=args.case,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "report": str(run_dir / "report.yaml")}
    print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), end="")
    return exit_code


def _handle_eval_run_task_session(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    _validate_case_args(args.case, args.cases)
    _warn_eval_full_access_repeat(loaded, args.repeat)
    service = EvalService(loaded=loaded)
    if args.cases is not None:
        run_dir, exit_code = service.run_task_session_cases(
            cases_dir=args.cases,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "summary": str(run_dir / "summary.yaml")}
    else:
        run_dir, exit_code = service.run_task_session(
            case_dir=args.case,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "report": str(run_dir / "report.yaml")}
    print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), end="")
    return exit_code


def _handle_eval_run_full_chain(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    _validate_case_args(args.case, args.cases)
    _warn_eval_full_access_repeat(loaded, args.repeat)
    service = EvalService(loaded=loaded)
    if args.cases is not None:
        run_dir, exit_code = service.run_full_chain_cases(
            cases_dir=args.cases,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "summary": str(run_dir / "summary.yaml")}
    else:
        run_dir, exit_code = service.run_full_chain(
            case_dir=args.case,
            label=args.label,
            dry_run_backend=args.dry_run_backend,
            repeat=args.repeat,
            allow_sensitive_config=args.allow_sensitive_config,
        )
        payload = {"run_dir": str(run_dir), "report": str(run_dir / "report.yaml")}
    print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), end="")
    return exit_code


def _handle_eval_promote(args: argparse.Namespace) -> int:
    loaded = load_evaluation_config(args.config)
    target_dir = EvalService(loaded=loaded).promote(
        eval_type=args.eval_type,
        run_dir=args.run,
        case_dir=args.case,
        review_path=args.review,
        name=args.name,
        allow_sensitive_config=args.allow_sensitive_config,
    )
    print(
        yaml.safe_dump(
            {"golden_case": str(target_dir)}, allow_unicode=True, sort_keys=False
        ),
        end="",
    )
    return 0


def _validate_case_args(case: Path | None, cases: Path | None) -> None:
    if (case is None) == (cases is None):
        raise EvalError("provide exactly one of --case or --cases")


def _warn_eval_full_access_repeat(loaded: LoadedConfig, repeat: int) -> None:
    if loaded.config.tool_permissions != "read_only" and repeat > 1:
        print(
            f"warning: {loaded.config.tool_permissions} agent tools may repeat external side effects across eval trials",
            file=sys.stderr,
        )


def _handle_retention_prune(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config, mode=args.mode)
    summary = RetentionService(
        store=store,
        config=loaded.config,
        base_dir=loaded.base_dir,
        logger=logger,
    ).prune(run_id=new_run_id("retention"), dry_run=args.dry_run)
    print(
        yaml.safe_dump(summary.as_dict(), allow_unicode=True, sort_keys=False), end=""
    )
    return 0


def _handle_reply_style_refresh(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config, mode=args.mode)
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    backend = create_agent_backend(loaded.config, base_dir=loaded.base_dir)
    refresher = ReplyStyleRefresher(
        config=loaded.config,
        store=store,
        base_dir=loaded.base_dir,
        feishu_client=client,
        agent_backend=backend,
        agent_invoker=AgentInvoker(
            logger=logger,
            max_attempts=loaded.config.agent_backend.max_attempts,
        ),
    )
    result = refresher.refresh(dry_run=args.dry_run, run_id=new_run_id("reply_style"))
    print(yaml.safe_dump(result.as_dict(), allow_unicode=True, sort_keys=False), end="")
    return 0 if result.status in {"dry_run", "written"} else 2


def _handle_maintenance_expire_approvals(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).expire_approvals(actor="local_cli")
    return _emit_command_result(result)


def _handle_policy_import_config(args: argparse.Namespace) -> int:
    loaded, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).import_policy_config(
        loaded.config,
        replace=args.replace,
        used_defaults=loaded.reply_policy_used_defaults,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_policy_sync_production(args: argparse.Namespace) -> int:
    if args.mode != "dry-run":
        print("policy sync-production requires --mode dry-run", file=sys.stderr)
        return 2
    _, store, _ = _load_runtime(args.config, mode="dry-run")
    result = sync_production_policy(ConfigService().load(args.config), store)
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_policy_update_global(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).update_global_policy(
        _global_policy_changes_from_args(args),
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_policy_update_chat(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).update_chat_policy(
        args.chat_id,
        _chat_policy_changes_from_args(args),
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_policy_delete_chat(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).delete_chat_policy(
        args.chat_id,
        actor="local_cli",
        reason=args.reason,
    )
    return _emit_command_result(result)


def _handle_dispatch_inspect(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).inspect_dispatch_action(
        args.action_id, actor="local_cli"
    )
    return _emit_command_result(result)


def _handle_dispatch_mark_sent(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config, mode=args.mode)
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    dispatcher = Dispatcher(
        store=store,
        feishu_client=client,
        config=loaded.config,
        logger=logger,
    )
    result = OperatorCommandService(
        store, readback_marker=dispatcher
    ).mark_dispatch_sent(
        args.action_id,
        sent_message_id=args.sent_message_id,
        actor="local_cli",
    )
    return _emit_command_result(result)


def _handle_dispatch_retry(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).retry_dispatch_action(
        args.action_id, actor="local_cli"
    )
    return _emit_command_result(result)


def _handle_dispatch_cancel(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config, mode=args.mode)
    result = OperatorCommandService(store).cancel_dispatch_action(
        args.action_id, actor="local_cli"
    )
    return _emit_command_result(result)


def _emit_command_result(result: CommandResult) -> int:
    print(yaml.safe_dump(result.as_dict(), allow_unicode=True, sort_keys=False), end="")
    return command_exit_code(result)


def _parse_bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _global_policy_changes_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "p2p_auto_reply": args.p2p_auto_reply,
        "unknown_group_auto_reply": args.unknown_group_auto_reply,
        "bot_joined": args.bot_joined,
        "reply_identity": args.reply_identity,
        "allow_user_fallback": args.allow_user_fallback,
        "resource_download": args.resource_download,
    }


def _chat_policy_changes_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "name": args.name,
        "auto_reply": args.auto_reply,
        "bot_joined": args.bot_joined,
        "reply_identity": args.reply_identity,
        "allow_user_fallback": args.allow_user_fallback,
        "resource_download": args.resource_download,
    }


def _load_runtime(
    config_path: str | None,
    *,
    mode: str | None = None,
) -> tuple[LoadedConfig, SQLiteStore, JSONLLogger]:
    loaded = ConfigService().load(config_path)
    production_loaded = loaded
    if mode == "dry-run":
        loaded = dry_run_config(loaded)
    sqlite_path = resolve_relative_path(
        loaded.config.storage.sqlite_path, loaded.base_dir
    )
    jsonl_path = resolve_relative_path(
        loaded.config.logging.jsonl_path, loaded.base_dir
    )
    text_path = (
        None
        if loaded.config.logging.text_path is None
        else resolve_relative_path(loaded.config.logging.text_path, loaded.base_dir)
    )
    logger = JSONLLogger(
        jsonl_path,
        level=loaded.config.logging.level,
        console=loaded.config.logging.console,
        text_path=text_path,
    )
    store = SQLiteStore(sqlite_path)
    if mode == "dry-run":
        initialize_dry_run_policy(production_loaded, store)
    return loaded, store, logger


class GitInfo(TypedDict):
    git_commit: str | None
    git_dirty: bool


def _git_info(cwd: Path) -> GitInfo:
    commit = _git_output(["git", "rev-parse", "--short", "HEAD"], cwd)
    dirty_output = _git_output(["git", "status", "--porcelain"], cwd)
    return {
        "git_commit": commit,
        "git_dirty": bool(dirty_output),
    }


def _git_output(argv: list[str], cwd: Path) -> str | None:
    try:
        # Git arguments are fixed by this diagnostic helper and shell=False is
        # used, so repository content is never interpreted as shell syntax.
        completed = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _watch_until_from_now(minutes: int) -> str:
    return shift_instant(utc_now_iso(), delta=timedelta(minutes=minutes))


def _run_console_server(app: Any, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
