from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
HERMES_V0232 = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
PATCHES = (
    "hermes-0.23.2-feishu-topic-replies.patch",
    "hermes-0.23.2-feishu-group-partners.patch",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test the Feishu partner patch against Hermes v0.23.2"
    )
    parser.add_argument("hermes_checkout", type=Path)
    args = parser.parse_args()
    git = shutil.which("git")
    tar = shutil.which("tar")
    if not git or not tar:
        parser.error("git and tar must be available on PATH")

    with tempfile.TemporaryDirectory(prefix="hermes-partner-test-") as temporary:
        scratch = Path(temporary)
        archive = scratch / "hermes.tar"
        subprocess.run(  # noqa: S603
            [
                git,
                "-C",
                str(args.hermes_checkout),
                "archive",
                "--format=tar",
                f"--output={archive}",
                HERMES_V0232,
            ],
            check=True,
        )
        subprocess.run([tar, "-xf", str(archive), "-C", str(scratch)], check=True)  # noqa: S603
        subprocess.run([git, "-C", str(scratch), "init", "--quiet"], check=True)  # noqa: S603
        for patch in PATCHES:
            subprocess.run(  # noqa: S603
                [
                    git,
                    "-C",
                    str(scratch),
                    "apply",
                    str(PROJECT / "docs/patches" / patch),
                ],
                check=True,
            )

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FEISHU_", "LARK_", "HERMES_", "GATEWAY_"))
        }
        env["HERMES_HOME"] = str(scratch / "home")
        env["PYTHONPATH"] = os.pathsep.join((str(scratch), str(PROJECT / "src")))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        return subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "pytest_asyncio.plugin",
                str(PROJECT / "docs/patches/tests/hermes_gateway_partners.py"),
            ],
            cwd=PROJECT,
            env=env,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
