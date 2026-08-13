from __future__ import annotations

import hmac
import ipaddress
import secrets
from urllib.parse import quote


def generate_console_token() -> str:
    return secrets.token_urlsafe(32)


def validate_console_bind_host(host: str) -> str:
    normalized = host.strip().lower()
    if not normalized:
        raise ValueError("console host must not be empty")
    if normalized == "localhost":
        return normalized
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "console host must be localhost or a loopback IP address"
        ) from exc
    if not address.is_loopback:
        raise ValueError("console host must be localhost or a loopback IP address")
    return normalized


def _loopback_host_aliases(host: str) -> set[str]:
    normalized = host.strip().lower()
    aliases = {normalized}
    if normalized in {"127.0.0.1", "localhost", "::1"}:
        aliases.update({"127.0.0.1", "localhost", "::1"})
    return aliases


def allowed_host_headers(host: str, port: int) -> set[str]:
    normalized = validate_console_bind_host(host)
    headers: set[str] = set()
    for alias in _loopback_host_aliases(normalized):
        if ":" in alias and not alias.startswith("["):
            host_header = f"[{alias}]"
        else:
            host_header = alias
        headers.add(host_header)
        headers.add(f"{host_header}:{port}")
    return headers


def host_header_allowed(host_header: str | None, *, host: str, port: int) -> bool:
    if host_header is None:
        return False
    return host_header.strip().lower() in allowed_host_headers(host, port)


def bearer_token_valid(authorization: str | None, expected_token: str) -> bool:
    if authorization is None:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token, expected_token)


def console_access_url(*, host: str, port: int, token: str) -> str:
    normalized = validate_console_bind_host(host)
    display_host = (
        f"[{normalized}]"
        if ":" in normalized and not normalized.startswith("[")
        else normalized
    )
    return f"http://{display_host}:{port}/#token={quote(token, safe='')}"
