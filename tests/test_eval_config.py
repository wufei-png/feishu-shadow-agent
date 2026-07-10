from __future__ import annotations

from pathlib import Path

from feishu_shadow_agent.config import ConfigService
from feishu_shadow_agent.evals.artifacts import copy_config_or_raise, write_metadata
from feishu_shadow_agent.evals.config import load_evaluation_config


def test_unmodified_eval_config_copy_preserves_original_base_dir(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = project / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    artifact = tmp_path / "artifact"
    config_info = copy_config_or_raise(
        loaded=loaded,
        destination_dir=artifact,
        allow_sensitive_config=False,
    )
    write_metadata(artifact, loaded=loaded, config_info=config_info)

    replay = load_evaluation_config(artifact / "config.yaml")

    assert replay.base_dir == project


def test_modified_eval_config_copy_uses_its_own_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = project / "config.yaml"
    config_path.write_text(
        Path("tests/fixtures/minimal.config.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loaded = ConfigService().load(config_path)
    artifact = tmp_path / "artifact"
    config_info = copy_config_or_raise(
        loaded=loaded,
        destination_dir=artifact,
        allow_sensitive_config=False,
    )
    write_metadata(artifact, loaded=loaded, config_info=config_info)
    copied = artifact / "config.yaml"
    copied.write_text(
        copied.read_text(encoding="utf-8").replace(
            "watch_minutes: 120", "watch_minutes: 121"
        ),
        encoding="utf-8",
    )

    replay = load_evaluation_config(copied)

    assert replay.base_dir == artifact
