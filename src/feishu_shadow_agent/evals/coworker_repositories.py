from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .artifacts import EvalError
from .coworker_dataset import InspectedCoworkerReview, ReviewRepository


@dataclass(frozen=True)
class MaterializedRepository:
    mount_name: str
    path: Path
    revision: str
    tree_sha1: str
    omitted_symlinks: tuple[str, ...]
    uninitialized_submodules: tuple[str, ...]


def materialize_coworker_repositories(
    review: InspectedCoworkerReview, case_id: str, destination: Path
) -> tuple[MaterializedRepository, ...]:
    """Copy the declared committed trees, without running a model or accessing a network.

    The caller owns the newly created destination. A future runner must mount these
    copies read-only inside an isolated filesystem; this function is not a sandbox.
    """
    case = next(
        (item for item in review.dataset.cases if item.case_id == case_id), None
    )
    if case is None:
        raise EvalError(f"unknown coworker case: {case_id}")
    destination = destination.resolve()
    if any(
        destination.is_relative_to(Path(item.repository).resolve())
        for item in case.scenario.read_only_repositories
    ):
        raise EvalError("coworker repository destination must be outside source repos")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise EvalError("coworker repository destination must be new") from exc
    try:
        return tuple(
            _materialize_repository(item, destination)
            for item in case.scenario.read_only_repositories
        )
    except BaseException:
        shutil.rmtree(destination)
        raise


def _materialize_repository(
    repository: ReviewRepository, destination: Path
) -> MaterializedRepository:
    source = Path(repository.repository)
    tree = _git(source, "rev-parse", "--verify", f"{repository.revision}^{{tree}}")
    if tree.strip() != repository.tree_sha1:
        raise EvalError(f"coworker repository tree mismatch: {repository.mount_name}")
    entries = _git(source, "ls-tree", "-r", "-z", repository.revision).split("\0")
    symlinks: list[str] = []
    submodules: list[str] = []
    for entry in filter(None, entries):
        metadata, separator, name = entry.partition("\t")
        if not separator:
            raise EvalError(f"invalid Git tree entry: {repository.mount_name}")
        mode = metadata.split(" ", 1)[0]
        if mode == "120000":
            symlinks.append(name)
        elif mode == "160000":
            submodules.append(name)

    checkout = destination / repository.mount_name
    _git(destination, "init", "--quiet", "--template=", str(checkout))
    _git(
        checkout,
        "-c",
        "protocol.file.allow=always",
        "fetch",
        "--quiet",
        "--no-tags",
        "--depth=1",
        "--no-recurse-submodules",
        str(source),
        repository.revision,
    )
    _git(
        checkout,
        "-c",
        "core.hooksPath=/dev/null",
        "checkout",
        "--detach",
        "--quiet",
        "--force",
        "FETCH_HEAD",
    )
    actual = _git(checkout, "rev-parse", "--verify", "HEAD").strip()
    if actual != repository.revision:
        raise EvalError(f"coworker repository commit mismatch: {repository.mount_name}")
    # FETCH_HEAD includes the original host path, which the agent does not need.
    (checkout / ".git" / "FETCH_HEAD").unlink(missing_ok=True)

    omitted: list[str] = []
    for name in symlinks:
        link = checkout / name
        if not link.is_symlink():
            raise EvalError(
                f"coworker repository symlink missing: {repository.mount_name}"
            )
        try:
            target = (link.parent / os.readlink(link)).resolve()
        except (OSError, RuntimeError) as exc:
            raise EvalError(f"invalid coworker repository symlink: {name}") from exc
        if not target.is_relative_to(checkout.resolve()):
            link.unlink()
            omitted.append(name)
    return MaterializedRepository(
        mount_name=repository.mount_name,
        path=checkout,
        revision=repository.revision,
        tree_sha1=repository.tree_sha1,
        omitted_symlinks=tuple(omitted),
        uninitialized_submodules=tuple(submodules),
    )


def _git(cwd: Path, *args: str) -> str:
    # Suppress user/system Git config and credential prompts in the copied repo.
    env = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(cwd), *args],  # noqa: S607
            env=env,
            capture_output=True,
            check=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvalError("cannot materialize coworker Git repository") from exc
    return os.fsdecode(result.stdout)
