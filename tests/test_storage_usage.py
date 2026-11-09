from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from feishu_shadow_agent.config import AppConfig, LoggingConfig, OwnerConfig
from feishu_shadow_agent.operator_queries.storage import StorageQuery
from feishu_shadow_agent.operator_query import OperatorQueryService
from feishu_shadow_agent.storage_usage import (
    directory_bytes,
    file_bytes,
    resolve_storage_usage,
)
from feishu_shadow_agent.store.sqlite_store import SQLiteStore


def _config() -> AppConfig:
    return AppConfig(owner=OwnerConfig(open_id="ou_owner", name="Owner"))


def _fixed_disk_usage(
    free_by_path: dict[str, int], *, total: int = 1_000, used: int = 200
) -> Callable[[Path], tuple[int, int, int]]:
    def disk_usage(path: Path) -> tuple[int, int, int]:
        return (total, used, free_by_path[str(Path(path))])

    return disk_usage


def test_storage_usage_reports_runtime_sizes(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "data" / "resources").mkdir()
    (tmp_path / "data" / "agent.sqlite3").write_bytes(b"x" * 100)
    (tmp_path / "logs" / "agent.jsonl").write_bytes(b"y" * 50)
    (tmp_path / "data" / "resources" / "a.bin").write_bytes(b"z" * 25)
    (tmp_path / "data" / "resources" / "b.bin").write_bytes(b"z" * 5)

    usage = resolve_storage_usage(
        _config(),
        tmp_path,
        disk_usage=_fixed_disk_usage(
            {
                str(tmp_path / "data"): 400,
                str(tmp_path / "data" / "resources"): 800,
                str(tmp_path / "logs"): 900,
            }
        ),
    )

    assert usage.sqlite_bytes == 100
    assert usage.jsonl_log_bytes == 50
    assert usage.text_log_bytes is None
    assert usage.resource_dir_bytes == 30
    assert usage.resource_dir_quota_bytes == _config().storage.max_resource_dir_bytes
    assert usage.filesystem_path == str(tmp_path / "data")
    assert usage.filesystem_total_bytes == 1_000
    assert usage.filesystem_free_bytes == 400


def test_storage_usage_reports_the_tightest_volume(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()

    usage = resolve_storage_usage(
        _config(),
        tmp_path,
        disk_usage=_fixed_disk_usage(
            {str(tmp_path / "data"): 900, str(tmp_path / "logs"): 12}
        ),
    )

    assert usage.filesystem_path == str(tmp_path / "logs")
    assert usage.filesystem_free_bytes == 12


def test_storage_usage_does_not_create_missing_paths(tmp_path: Path) -> None:
    usage = resolve_storage_usage(
        _config(),
        tmp_path,
        disk_usage=_fixed_disk_usage({str(tmp_path): 5_000}),
    )

    assert usage.sqlite_bytes == 0
    assert usage.jsonl_log_bytes == 0
    assert usage.resource_dir_bytes == 0
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "logs").exists()


def test_directory_bytes_skips_symlinks_and_directories(tmp_path: Path) -> None:
    root = tmp_path / "resources"
    root.mkdir()
    target = root / "file.bin"
    target.write_bytes(b"x" * 40)
    (root / "nested").mkdir()
    (root / "nested" / "inner.bin").write_bytes(b"y" * 10)
    (root / "link.bin").symlink_to(target)

    assert directory_bytes(root) == 50


def test_file_bytes_follows_a_symlinked_file(tmp_path: Path) -> None:
    target = tmp_path / "agent.sqlite3"
    target.write_bytes(b"x" * 64)
    (tmp_path / "agent.sqlite3.link").symlink_to(target)

    assert file_bytes(tmp_path / "agent.sqlite3.link") == 64
    assert file_bytes(tmp_path / "missing.sqlite3") == 0


def test_storage_usage_reports_a_configured_text_log(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "agent.log").write_bytes(b"t" * 7)
    config = AppConfig(
        owner=OwnerConfig(open_id="ou_owner", name="Owner"),
        logging=LoggingConfig(text_path="logs/agent.log"),
    )

    usage = resolve_storage_usage(
        config,
        tmp_path,
        disk_usage=_fixed_disk_usage({str(tmp_path): 9, str(tmp_path / "logs"): 1}),
    )

    assert usage.text_log_bytes == 7


def test_storage_query_hides_storage_when_measurement_fails(tmp_path: Path) -> None:
    def broken(path: Path) -> tuple[int, int, int]:
        raise OSError("statvfs failed")

    query = StorageQuery(config=_config(), base_dir=tmp_path, disk_usage=broken)

    assert query.storage_summary() is None


def test_dashboard_includes_storage_when_runtime_config_is_bound(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "data" / "agent.sqlite3")
    store.initialize()

    bound = OperatorQueryService(
        store, policy_import_source=_config(), base_dir=tmp_path
    ).dashboard_snapshot()
    unbound = OperatorQueryService(store).dashboard_snapshot()

    assert bound["storage"]["sqlite_bytes"] > 0
    assert bound["storage"]["filesystem_free_bytes"] > 0
    assert "storage" not in unbound
