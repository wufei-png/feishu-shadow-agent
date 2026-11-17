from __future__ import annotations

import subprocess
import sys


def test_normalization_can_be_imported_without_loading_ingestion() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import feishu_shadow_agent.message_normalization; "
            "assert 'feishu_shadow_agent.ingestion' not in sys.modules",
        ],
        check=True,
    )
