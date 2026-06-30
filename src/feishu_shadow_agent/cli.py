from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

import yaml

from .config import ConfigError, ConfigService, LoadedConfig
from .daemon import Daemon
from .dispatcher import Dispatcher
from .feishu.lark_cli import LarkCliClient
from .health import HealthSuite, has_critical_failure, summarize_results
from .hermes import HermesCliClient
from .jsonl import JSONLLogger
from .paths import resolve_agent_skill_path, resolve_relative_path
from .processing import TaskProcessingService
from .retention import RetentionService
from .store.sqlite_store import SQLiteStore
from .types import new_run_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu_shadow_agent")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="run health checks")
    _add_config_arg(doctor)
    doctor.add_argument("--send-test", action="store_true", help="send a real test DM to owner")
    doctor.set_defaults(handler=_handle_doctor)

    daemon = subparsers.add_parser("daemon", help="run the daemon", description="run the daemon")
    _add_config_arg(daemon)
    daemon.add_argument(
        "--dry-run",
        action="store_true",
        help="do not send external replies; record local state and dispatch previews",
    )
    daemon.add_argument(
        "--send-owner-notifications",
        action="store_true",
        help="with --dry-run, actually send and consume owner_notification actions; external replies stay pending",
    )
    daemon.set_defaults(handler=_handle_daemon)

    config = subparsers.add_parser("config", help="configuration helpers")
    config_subparsers = config.add_subparsers(dest="config_command")
    config_show = config_subparsers.add_parser("show", help="print config")
    _add_config_arg(config_show)
    config_show.add_argument("--redacted", action="store_true", help="redact secret-like fields")
    config_show.set_defaults(handler=_handle_config_show)
    config_schema = config_subparsers.add_parser("schema", help="print config JSON schema")
    config_schema.set_defaults(handler=_handle_config_schema)
    config_validate = config_subparsers.add_parser("validate", help="validate config without runtime health checks")
    _add_config_arg(config_validate)
    config_validate.set_defaults(handler=_handle_config_validate)

    status = subparsers.add_parser("status", help="show daemon and queue status")
    _add_config_arg(status)
    status.set_defaults(handler=_handle_status)

    replay = subparsers.add_parser("replay", help="explain local state and preview dispatch")
    _add_config_arg(replay)
    replay.add_argument("--message-id", required=True)
    replay.add_argument("--dry-run", action="store_true", required=True)
    replay.set_defaults(handler=_handle_replay)

    retention = subparsers.add_parser("retention", help="retention helpers")
    retention_subparsers = retention.add_subparsers(dest="retention_command")
    retention_prune = retention_subparsers.add_parser("prune", help="prune expired local data")
    _add_config_arg(retention_prune)
    retention_prune.add_argument("--dry-run", action="store_true", help="preview retention cleanup")
    retention_prune.set_defaults(handler=_handle_retention_prune)

    maintenance = subparsers.add_parser("maintenance", help="explicit maintenance helpers")
    maintenance_subparsers = maintenance.add_subparsers(dest="maintenance_command")
    maintenance_expire_approvals = maintenance_subparsers.add_parser(
        "expire-approvals",
        help="expire overdue pending approvals",
    )
    _add_config_arg(maintenance_expire_approvals)
    maintenance_expire_approvals.set_defaults(handler=_handle_maintenance_expire_approvals)

    dispatch = subparsers.add_parser("dispatch", help="dispatch recovery helpers")
    dispatch_subparsers = dispatch.add_subparsers(dest="dispatch_command")
    dispatch_inspect = dispatch_subparsers.add_parser("inspect", help="inspect an action and dispatch attempts")
    _add_config_arg(dispatch_inspect)
    dispatch_inspect.add_argument("--action-id", type=int, required=True)
    dispatch_inspect.set_defaults(handler=_handle_dispatch_inspect)
    dispatch_mark_sent = dispatch_subparsers.add_parser("mark-sent", help="verify readback and mark an action sent")
    _add_config_arg(dispatch_mark_sent)
    dispatch_mark_sent.add_argument("--action-id", type=int, required=True)
    dispatch_mark_sent.add_argument("--sent-message-id", required=True)
    dispatch_mark_sent.set_defaults(handler=_handle_dispatch_mark_sent)
    dispatch_retry = dispatch_subparsers.add_parser("retry", help="requeue a failed dispatch action")
    _add_config_arg(dispatch_retry)
    dispatch_retry.add_argument("--action-id", type=int, required=True)
    dispatch_retry.set_defaults(handler=_handle_dispatch_retry)
    dispatch_cancel = dispatch_subparsers.add_parser("cancel", help="cancel a dispatch action")
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
    send.add_argument("--stdin", action="store_true", dest="read_stdin", help="read final reply from stdin")
    send.add_argument("task_id")
    send.add_argument("text", nargs=argparse.REMAINDER)
    send.set_defaults(handler=_handle_send)

    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to config.yaml")


def _handle_doctor(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config)
    run_id = new_run_id("doctor")
    store.record_run_start(run_id=run_id, dry_run=not args.send_test, **_git_info(Path.cwd()))
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    suite = HealthSuite(loaded_config=loaded, store=store, feishu_client=client, run_id=run_id)
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
    loaded, store, logger = _load_runtime(args.config)
    run_id = new_run_id("run")
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    suite = HealthSuite(loaded_config=loaded, store=store, feishu_client=client, run_id=run_id)
    backend_config = loaded.config.agent_backend
    session_skills = [
        resolve_agent_skill_path(skill, loaded.base_dir)
        for skill in backend_config.explicit_context.skills
    ]
    agent_backend = HermesCliClient(
        config=backend_config.hermes,
        tool_permissions=loaded.config.tool_permissions,
        config_scope=backend_config.config_scope,
        auto_context=backend_config.auto_context,
        session_skills=session_skills,
        cwd=loaded.base_dir,
    )
    task_processor = TaskProcessingService(
        store=store,
        config=loaded.config,
        agent_backend=agent_backend,
        logger=logger,
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
        send_owner_notifications=args.send_owner_notifications,
        run_metadata=_git_info(Path.cwd()),
        config_base_dir=loaded.base_dir,
    )
    return daemon.run_forever()


def _handle_config_show(args: argparse.Namespace) -> int:
    service = ConfigService()
    loaded = service.load(args.config)
    data = service.redacted_dict(loaded.config) if args.redacted else loaded.config.model_dump(mode="json")
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
    _, store, _ = _load_runtime(args.config)
    print(yaml.safe_dump(store.status_snapshot(), allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_approve(args: argparse.Namespace) -> int:
    return _handle_local_approval_command(args.config, verb="approve", target_id=args.approval_id)


def _handle_reject(args: argparse.Namespace) -> int:
    return _handle_local_approval_command(args.config, verb="reject", target_id=args.approval_id)


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
    )


def _handle_local_approval_command(
    config_path: str | None,
    *,
    verb: str,
    target_id: str,
    final_reply: str | None = None,
) -> int:
    _, store, _ = _load_runtime(config_path)
    command = f"/{verb} {target_id}" if final_reply is None else f"/{verb} {target_id} {final_reply}"
    result = store.apply_approval_command(
        message_id=new_run_id(f"local_{verb}"),
        command=command,
        verb=verb,
        target_id=target_id,
        final_reply=final_reply,
    )
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end="")
    return 0 if result.get("status") == "applied" else 2


def _handle_replay(args: argparse.Namespace) -> int:
    loaded, store, _ = _load_runtime(args.config)
    client = LarkCliClient(
        path=loaded.config.lark_cli.path,
        timeout_seconds=loaded.config.lark_cli.timeout_seconds,
        cwd=loaded.base_dir,
    )
    previews = []
    with tempfile.TemporaryDirectory(prefix="feishu-shadow-agent-replay-") as tmp:
        temp_db = Path(tmp) / "agent.sqlite3"
        if store.path.exists():
            shutil.copy2(store.path, temp_db)
        temp_store = SQLiteStore(temp_db)
        temp_store.migrate()
        summary = temp_store.replay_summary(args.message_id)
        if summary is None:
            print(f"message not found: {args.message_id}", file=sys.stderr)
            return 2
        related_pending_action_ids = [
            action["id"]
            for action in summary["actions"]
            if action.get("status") == "pending" and action.get("kind") in {"send_reply", "owner_notification"}
        ]
        dispatcher = Dispatcher(
            store=temp_store,
            feishu_client=client,
            config=loaded.config,
            logger=JSONLLogger(Path(tmp) / "replay.jsonl"),
        )
        run_id = new_run_id("replay")
        for action_id in related_pending_action_ids:
            preview = dispatcher.preview_action(action_id, run_id=run_id)
            if preview is not None:
                previews.append(preview)
    output = {
        "message_id": args.message_id,
        "state": summary,
        "dispatch_preview": {
            "processed": len(previews),
            "actions": previews,
        },
        "mutated_real_db": False,
    }
    print(yaml.safe_dump(output, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_retention_prune(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config)
    summary = RetentionService(
        store=store,
        config=loaded.config,
        base_dir=loaded.base_dir,
        logger=logger,
    ).prune(run_id=new_run_id("retention"), dry_run=args.dry_run)
    print(yaml.safe_dump(summary.as_dict(), allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_maintenance_expire_approvals(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config)
    expired = store.expire_pending_approvals()
    print(yaml.safe_dump({"expired_approvals": expired}, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_dispatch_inspect(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config)
    inspection = store.get_dispatch_inspection(args.action_id)
    if inspection is None:
        print(f"action not found: {args.action_id}", file=sys.stderr)
        return 2
    print(yaml.safe_dump(inspection, allow_unicode=True, sort_keys=False), end="")
    return 0


def _handle_dispatch_mark_sent(args: argparse.Namespace) -> int:
    loaded, store, logger = _load_runtime(args.config)
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
    result = dispatcher.mark_action_sent_after_readback(
        args.action_id,
        sent_message_id=args.sent_message_id,
        run_id=new_run_id("dispatch_recovery"),
    )
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end="")
    return 0 if result.get("status") == "sent" else 2


def _handle_dispatch_retry(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config)
    try:
        action = store.retry_dispatch_action(args.action_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        yaml.safe_dump(
            {"status": "requeued", "action": _action_output(action)},
            allow_unicode=True,
            sort_keys=False,
        ),
        end="",
    )
    return 0


def _handle_dispatch_cancel(args: argparse.Namespace) -> int:
    _, store, _ = _load_runtime(args.config)
    try:
        action = store.cancel_dispatch_action(args.action_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        yaml.safe_dump(
            {"status": "cancelled", "action": _action_output(action)},
            allow_unicode=True,
            sort_keys=False,
        ),
        end="",
    )
    return 0


def _action_output(action: object) -> dict[str, object]:
    data = getattr(action, "__dict__", {}).copy()
    return data if isinstance(data, dict) else {}


def _load_runtime(config_path: str | None) -> tuple[LoadedConfig, SQLiteStore, JSONLLogger]:
    loaded = ConfigService().load(config_path)
    sqlite_path = resolve_relative_path(loaded.config.storage.sqlite_path, loaded.base_dir)
    jsonl_path = resolve_relative_path(loaded.config.logging.jsonl_path, loaded.base_dir)
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
    return loaded, SQLiteStore(sqlite_path), logger


def _git_info(cwd: Path) -> dict[str, object]:
    commit = _git_output(["git", "rev-parse", "--short", "HEAD"], cwd)
    dirty_output = _git_output(["git", "status", "--porcelain"], cwd)
    return {
        "git_commit": commit,
        "git_dirty": bool(dirty_output),
    }


def _git_output(argv: list[str], cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
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
