from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from feishu_shadow_agent.time_utils import (
    format_instant,
    normalize_instant,
    parse_instant,
    parse_instant_or_none,
    shift_instant,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-14T12:30:45+08:00", "2026-08-14T04:30:45+00:00"),
        ("2026-08-14T04:30:45Z", "2026-08-14T04:30:45+00:00"),
        ("2026-08-13T21:30:45-07:00", "2026-08-14T04:30:45+00:00"),
    ],
)
def test_normalize_instant_uses_canonical_utc(value: str, expected: str) -> None:
    assert normalize_instant(value) == expected


@pytest.mark.parametrize("value", ["2026-08-14T12:30:45", "not-a-timestamp", ""])
def test_parse_instant_rejects_non_instant_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_instant(value)
    assert parse_instant_or_none(value) is None


def test_format_instant_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        format_instant(
            datetime(2026, 8, 14, 12, 30, 45, tzinfo=UTC).replace(tzinfo=None)
        )


def test_normalize_instant_preserves_subsecond_ordering() -> None:
    assert normalize_instant("2026-08-14T12:30:45.000001+08:00") == (
        "2026-08-14T04:30:45.000001+00:00"
    )


def test_shift_instant_is_elapsed_time_across_dst_boundary() -> None:
    assert (
        shift_instant("2026-03-08T01:30:00-08:00", delta=timedelta(hours=2))
        == "2026-03-08T11:30:00+00:00"
    )


@pytest.mark.parametrize(
    "process_timezone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"]
)
def test_feishu_local_timestamp_normalization_ignores_process_timezone(
    process_timezone: str,
) -> None:
    code = (
        "from feishu_shadow_agent.ingestion import normalize_message_sent_at; "
        "print(normalize_message_sent_at('2026-08-14 12:30'))"
    )
    # The test launches the current interpreter with a fixed inline snippet.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env={**os.environ, "TZ": process_timezone},
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "2026-08-14T04:30:00+00:00"
