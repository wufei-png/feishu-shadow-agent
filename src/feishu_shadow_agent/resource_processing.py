from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4

from .config import AppConfig
from .jsonl import JSONLLogger
from .membership import classify_bot_membership_absence, record_bot_membership_absence
from .policy import PolicyResolver
from .storage_usage import directory_bytes
from .store.sqlite_store import SQLiteStore
from .types import LarkCliResult, NormalizedMessage, ResourceRef, ResourceStatus


class ResourceDownloadClient(Protocol):
    download_resource: Callable[..., LarkCliResult]


PARTIAL_RESOURCE_PATTERN = re.compile(r"^.+\.bin\.part-[0-9a-f]{32}$")


@dataclass(frozen=True)
class ResourceQuotaDecision:
    allow: bool
    status: str | None = None
    raw: dict[str, Any] | None = None


class ResourceProcessor:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        feishu_client: ResourceDownloadClient,
        config: AppConfig,
        logger: JSONLLogger,
        config_base_dir: str | Path | None = None,
        resource_base_dir: str | Path | None = None,
        store_absolute_paths: bool = False,
        preserve_resource_base_path: bool = False,
    ):
        self.store = store
        self.feishu_client = feishu_client
        self.config = config
        self.logger = logger
        self.config_base_dir = (
            Path(config_base_dir or Path.cwd()).expanduser().resolve()
        )
        resource_base = Path(resource_base_dir or self.config_base_dir).expanduser()
        self.resource_base_dir = (
            resource_base.absolute()
            if preserve_resource_base_path
            else resource_base.resolve()
        )
        self.store_absolute_paths = store_absolute_paths
        self.policy = PolicyResolver(store)
        self.quota = ResourceQuotaGuard(config=config, base_dir=self.resource_base_dir)
        self._quota_downloads_blocked = False

    def process(self, message: NormalizedMessage, *, run_id: str | None = None) -> None:
        if not message.resources:
            return
        if message.chat_type == "group" and (
            not message.chat_id
            or self.store.get_chat_product_policy(message.chat_id) is None
        ):
            return
        stale_parts_deleted = self.quota.cleanup_stale_partial_downloads()
        if stale_parts_deleted:
            self.logger.info(
                "stale_partial_resources_deleted",
                run_id=run_id,
                data={"count": stale_parts_deleted},
            )
        existing_resources = {
            (str(row["file_key"]), str(row["resource_type"])): row
            for row in self.store.list_resources_for_messages([message.message_id])
        }
        resource_policy = self.policy.can_download_resources(message)
        for resource in message.resources:
            if (
                not resource_policy.allow
                and resource_policy.reason == "disabled_by_chat_policy"
            ):
                self.store.upsert_resource(
                    resource,
                    download_status="skipped",
                    raw={
                        "reason": "disabled_by_chat_policy",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                self.logger.info(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "disabled_by_chat_policy",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                continue
            if not resource_policy.allow and resource_policy.reason == "bot_not_joined":
                # Resource download is bot-only for user messages. Recording the
                # gate here avoids repeated 234040/234002 calls and lets reply
                # policy decide whether the task can proceed without the asset.
                self.store.upsert_resource(
                    resource,
                    download_status="bot_not_joined",
                    raw={
                        "reason": "chat_policy_bot_joined_false",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                self.logger.warning(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "bot_not_joined",
                        "policy_source": resource_policy.policy_source,
                    },
                )
                continue
            if self._quota_downloads_blocked:
                self._record_quota_blocked(
                    resource,
                    run_id=run_id,
                    decision=ResourceQuotaDecision(
                        allow=False,
                        status=ResourceStatus.QUOTA_EXCEEDED.value,
                        raw={
                            "reason": "resource_quota_exceeded",
                            "blocked_after_prior_quota": True,
                        },
                    ),
                )
                continue
            output = _resource_output(resource, self.config.storage.resource_dir)
            local_output = self.resource_base_dir / output
            stored_path = str(local_output) if self.store_absolute_paths else output
            existing = existing_resources.get(
                (resource.file_key, resource.resource_type)
            )
            if self._verified_existing_download(
                existing,
                local_output=local_output,
                stored_path=stored_path,
            ):
                self.logger.debug(
                    "resource_download_skipped",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "reason": "already_downloaded",
                    },
                )
                continue
            quota_preflight = self.quota.before_download()
            if not quota_preflight.allow:
                self._record_quota_blocked(
                    resource, run_id=run_id, decision=quota_preflight
                )
                self._quota_downloads_blocked = True
                continue
            self.logger.debug(
                "resource_download_started",
                run_id=run_id,
                data={
                    "message_id": resource.message_id,
                    "file_key": resource.file_key,
                    "resource_type": resource.resource_type,
                    "output": output,
                },
            )
            temporary_output = f"{output}.part-{uuid4().hex}"
            temporary_path = self.resource_base_dir / temporary_output
            try:
                self.quota.validate_download_target(local_output)
                self.quota.validate_download_target(temporary_path)
                local_output.parent.mkdir(parents=True, exist_ok=True)
                self.quota.validate_download_target(local_output)
                self.quota.validate_download_target(temporary_path)
                if message.chat_type == "group" and (
                    not message.chat_id
                    or self.store.get_chat_product_policy(message.chat_id) is None
                ):
                    return
                result = self.feishu_client.download_resource(
                    message_id=resource.message_id,
                    file_key=resource.file_key,
                    resource_type=resource.resource_type,
                    output=temporary_output,
                )
            except Exception as exc:  # noqa: BLE001
                # Feishu resource clients can expose transport-specific
                # exceptions; mark this resource failed and continue ingestion.
                self.quota.delete_downloaded_file(temporary_path)
                self.store.upsert_resource(
                    resource,
                    download_status="failed",
                    path=None,
                    raw={"error": str(exc)},
                )
                self.logger.warning(
                    "resource_download_failed",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "error": str(exc),
                    },
                )
                continue
            result_json = _normalized_download_result(
                result.json_data,
                temporary_output=temporary_output,
                final_output=output,
            )
            try:
                self.quota.validate_download_target(temporary_path)
            except ValueError as exc:
                self.quota.delete_downloaded_file(temporary_path)
                self.store.upsert_resource(
                    resource,
                    download_status="failed",
                    path=None,
                    raw={"error": str(exc), "phase": "download_validation"},
                )
                self.logger.warning(
                    "resource_download_failed",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "error": str(exc),
                        "phase": "download_validation",
                    },
                )
                continue
            if result.ok:
                if not temporary_path.exists() or not temporary_path.is_file():
                    self.store.upsert_resource(
                        resource,
                        download_status="missing_file",
                        path=None,
                        raw={
                            "result": result_json,
                            "error": "download output file missing",
                        },
                    )
                    self.logger.warning(
                        "resource_download_missing_file",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "path": output,
                        },
                    )
                    continue
                quota_result = self.quota.after_download(
                    temporary_path,
                    attempted_path=output,
                    replacing_path=local_output,
                )
                if not quota_result.allow:
                    raw = {"result": result_json} | (quota_result.raw or {})
                    self._record_quota_blocked(
                        resource, run_id=run_id, decision=quota_result, raw=raw
                    )
                    if quota_result.status == ResourceStatus.QUOTA_EXCEEDED.value:
                        self._quota_downloads_blocked = True
                    continue
                sha256_hex = _sha256_if_exists(temporary_path)
                if sha256_hex is None:
                    self.store.upsert_resource(
                        resource,
                        download_status="missing_file",
                        path=None,
                        raw={
                            "result": result_json,
                            "error": "download output file missing",
                        },
                    )
                    self.logger.warning(
                        "resource_download_missing_file",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "path": output,
                        },
                    )
                    continue
                try:
                    _publish_download(temporary_path, local_output)
                except OSError as exc:
                    self.quota.delete_downloaded_file(temporary_path)
                    self.store.upsert_resource(
                        resource,
                        download_status="failed",
                        path=None,
                        raw={"error": str(exc), "phase": "atomic_publish"},
                    )
                    self.logger.warning(
                        "resource_download_failed",
                        run_id=run_id,
                        data={
                            "message_id": resource.message_id,
                            "file_key": resource.file_key,
                            "error": str(exc),
                            "phase": "atomic_publish",
                        },
                    )
                    continue
                self.store.upsert_resource(
                    resource,
                    download_status="downloaded",
                    path=stored_path,
                    sha256_hex=sha256_hex,
                    raw={"result": result_json},
                )
                self.logger.info(
                    "resource_downloaded",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "path": output,
                        "sha256": sha256_hex,
                    },
                )
            else:
                self.quota.delete_downloaded_file(temporary_path)
                membership_absence = classify_bot_membership_absence(result)
                status = "bot_invisible" if membership_absence else "failed"
                self.store.upsert_resource(
                    resource,
                    download_status=status,
                    path=None,
                    raw={
                        "error": result.error,
                        "stderr": result.stderr,
                        "stdout": result.stdout,
                    },
                )
                self.logger.warning(
                    "resource_download_unavailable",
                    run_id=run_id,
                    data={
                        "message_id": resource.message_id,
                        "file_key": resource.file_key,
                        "resource_type": resource.resource_type,
                        "status": status,
                        "error": result.error,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                    },
                )
                if membership_absence:
                    record_bot_membership_absence(
                        store=self.store,
                        config=self.config,
                        logger=self.logger,
                        chat_id=message.chat_id,
                        chat_type=message.chat_type,
                        run_id=run_id,
                        source="resource_download_failure",
                        error=result.error or result.stderr,
                        absence=membership_absence,
                    )

    def _verified_existing_download(
        self,
        existing: Any,
        *,
        local_output: Path,
        stored_path: str,
    ) -> bool:
        if (
            existing is None
            or existing["download_status"] != "downloaded"
            or existing["path"] != stored_path
            or not existing["sha256"]
        ):
            return False
        try:
            self.quota.validate_download_target(local_output)
        except ValueError:
            return False
        if local_output.is_symlink() or not local_output.is_file():
            return False
        return _sha256_if_exists(local_output) == existing["sha256"]

    def _record_quota_blocked(
        self,
        resource: ResourceRef,
        *,
        run_id: str | None,
        decision: ResourceQuotaDecision,
        raw: dict[str, Any] | None = None,
    ) -> None:
        status = decision.status or ResourceStatus.QUOTA_EXCEEDED.value
        payload = (decision.raw or {}) | (raw or {})
        self.store.upsert_resource(
            resource,
            download_status=status,
            path=None,
            raw=payload,
        )
        self.logger.warning(
            "resource_download_blocked_by_quota",
            run_id=run_id,
            data={
                "message_id": resource.message_id,
                "file_key": resource.file_key,
                "resource_type": resource.resource_type,
                "status": status,
                "details": payload,
            },
        )


class ResourceQuotaGuard:
    def __init__(self, *, config: AppConfig, base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        self.resource_path = (base_dir / config.storage.resource_dir).absolute()
        self.resource_root = self.resource_path.resolve(strict=False)

    def validate_download_target(self, path: Path) -> None:
        try:
            relative = path.absolute().relative_to(self.resource_path)
        except ValueError as exc:
            raise ValueError("download target is outside resource directory") from exc
        current = self.resource_path
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("download target contains a symbolic link")
        try:
            path.resolve(strict=False).relative_to(self.resource_root)
        except ValueError as exc:
            raise ValueError("download target is outside resource directory") from exc

    def before_download(self) -> ResourceQuotaDecision:
        usage = self.resource_dir_usage_bytes()
        if usage >= self.config.storage.max_resource_dir_bytes:
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.QUOTA_EXCEEDED.value,
                raw={
                    "reason": "resource_quota_exceeded",
                    "resource_dir_bytes": usage,
                    "max_resource_dir_bytes": self.config.storage.max_resource_dir_bytes,
                },
            )
        return ResourceQuotaDecision(allow=True)

    def cleanup_stale_partial_downloads(self, *, now: float | None = None) -> int:
        if not self.resource_root.exists():
            return 0
        stale_after_seconds = max(300, self.config.lark_cli.timeout_seconds * 2)
        stale_before = (time.time() if now is None else now) - stale_after_seconds
        deleted = 0
        for path in self.resource_root.rglob("*.part-*"):
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or PARTIAL_RESOURCE_PATTERN.fullmatch(path.name) is None
                    or path.stat().st_mtime > stale_before
                ):
                    continue
                path.unlink()
            except OSError:
                continue
            deleted += 1
        return deleted

    def after_download(
        self,
        path: Path,
        *,
        attempted_path: str,
        replacing_path: Path | None = None,
    ) -> ResourceQuotaDecision:
        size = path.stat().st_size
        if size > self.config.storage.max_resource_bytes:
            delete_error = self.delete_downloaded_file(path)
            raw = {
                "reason": "resource_too_large",
                "attempted_path": attempted_path,
                "size_bytes": size,
                "max_resource_bytes": self.config.storage.max_resource_bytes,
            }
            if delete_error:
                raw["delete_error"] = delete_error
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.TOO_LARGE.value,
                raw=raw,
            )
        replaced_size = 0
        if replacing_path is not None and replacing_path != path:
            self.validate_download_target(replacing_path)
            if not replacing_path.is_symlink() and replacing_path.is_file():
                replaced_size = replacing_path.stat().st_size
        usage = self.resource_dir_usage_bytes() - replaced_size
        if usage > self.config.storage.max_resource_dir_bytes:
            delete_error = self.delete_downloaded_file(path)
            raw = {
                "reason": "resource_quota_exceeded",
                "attempted_path": attempted_path,
                "size_bytes": size,
                "resource_dir_bytes": usage,
                "max_resource_dir_bytes": self.config.storage.max_resource_dir_bytes,
            }
            if delete_error:
                raw["delete_error"] = delete_error
            return ResourceQuotaDecision(
                allow=False,
                status=ResourceStatus.QUOTA_EXCEEDED.value,
                raw=raw,
            )
        return ResourceQuotaDecision(allow=True)

    def resource_dir_usage_bytes(self) -> int:
        return directory_bytes(self.resource_root)

    def delete_downloaded_file(self, path: Path) -> str | None:
        try:
            path.absolute().relative_to(self.resource_path)
        except ValueError:
            return "outside_resource_dir"
        if not path.is_symlink():
            try:
                path.resolve(strict=False).relative_to(self.resource_root)
            except ValueError:
                return "outside_resource_dir"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return str(exc)
        return None


def _resource_output(resource: ResourceRef, resource_dir: str) -> str:
    message_part = _safe_path_part(resource.message_id)
    key_hash = sha256(resource.file_key.encode("utf-8")).hexdigest()[:12]
    return PurePosixPath(
        resource_dir, message_part, f"{resource.resource_type}_{key_hash}.bin"
    ).as_posix()


def _safe_path_part(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "_" for char in value
    )[:120]


def _sha256_if_exists(path: str | Path) -> str | None:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    digest = sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_download(temporary_path: Path, final_path: Path) -> None:
    """Durably publish a validated download without exposing partial contents."""
    with temporary_path.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary_path, final_path)
    try:
        directory_fd = os.open(final_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        with suppress(OSError):
            # Some filesystems do not support directory fsync; the atomic
            # replace has still completed and the validated file is usable.
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _normalized_download_result(
    result: Any,
    *,
    temporary_output: str,
    final_output: str,
) -> Any:
    if not isinstance(result, dict):
        return result
    result_map = cast(dict[str, Any], result)
    if result_map.get("output") != temporary_output:
        return result_map
    return result_map | {"output": final_output}
