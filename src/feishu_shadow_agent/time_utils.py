from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_instant(value: str) -> datetime:
    """Parse an offset-aware ISO 8601 timestamp and return the UTC instant."""
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def parse_instant_or_none(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_instant(value)
    except ValueError:
        return None


def format_instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    utc_value = value.astimezone(UTC)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec)


def normalize_instant(value: str) -> str:
    return format_instant(parse_instant(value))


def shift_instant(value: str, *, delta: timedelta) -> str:
    return format_instant(parse_instant(value) + delta)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return format_instant(utc_now().replace(microsecond=0))
