from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status


LOCAL_BINDING_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
BIND_HOST_ENV = "CASCADE_BIND_HOST"
STRICT_AUTH_ENV = "CASCADE_STRICT_AUTH"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BindingGuardReport:
    host: str
    auth_enabled: bool
    local_binding: bool
    strict_auth: bool
    exposed_unauthenticated: bool
    refused: bool


def binding_guard(
    settings: Any = None,
    *,
    host: str | None = None,
    auth_enabled: bool | None = None,
    logger: logging.Logger | None = None,
) -> BindingGuardReport:
    """Guard against unauthenticated services bound beyond loopback.

    Resolves the effective bind host from ``host``, then the ``CASCADE_BIND_HOST``
    environment variable, then ``127.0.0.1``. Auth state comes from ``auth_enabled``
    or ``settings.enabled`` / ``settings.cascade_auth_enabled`` when present.
    A non-local bind with auth disabled always emits a CRITICAL log; when
    ``CASCADE_STRICT_AUTH`` is truthy it also raises ``RuntimeError`` to refuse boot.
    """
    resolved_host = (host if host is not None else os.environ.get(BIND_HOST_ENV, "")) or "127.0.0.1"
    effective_logger = logger or logging.getLogger("cascade.security.binding")

    if auth_enabled is None:
        explicit_enabled = getattr(settings, "enabled", None)
        if explicit_enabled is not None:
            auth_enabled = bool(explicit_enabled)
        else:
            auth_enabled = bool(getattr(settings, "cascade_auth_enabled", False))

    normalized_host = resolved_host.strip().lower()
    local_binding = normalized_host in LOCAL_BINDING_HOSTS
    strict_auth = os.environ.get(STRICT_AUTH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    exposed_unauthenticated = not local_binding and not auth_enabled
    refused = False

    if exposed_unauthenticated:
        effective_logger.critical(
            "CRITICAL: service binds to non-local host %r with authentication disabled "
            "(%s=false). Set %s=true to refuse boot, or enable API-key auth.",
            resolved_host,
            STRICT_AUTH_ENV,
            STRICT_AUTH_ENV,
        )
        if strict_auth:
            refused = True
            raise RuntimeError(
                f"Refusing to start: bound to non-local host {resolved_host!r} with auth disabled "
                f"while {STRICT_AUTH_ENV} is enabled."
            )

    return BindingGuardReport(
        host=resolved_host,
        auth_enabled=auth_enabled,
        local_binding=local_binding,
        strict_auth=strict_auth,
        exposed_unauthenticated=exposed_unauthenticated,
        refused=refused,
    )


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool = False
    local_demo_bypass: bool = False
    api_keys: str = ""
    api_key_hashes: str = ""
    auth_header: str = "Authorization"


def auth_status(settings: AuthSettings) -> dict[str, Any]:
    return {
        "auth_enabled": settings.enabled,
        "local_demo_auth_bypass": settings.local_demo_bypass,
        "auth_header": settings.auth_header,
        "configured_keys": len(_split(settings.api_keys)) + len(_split(settings.api_key_hashes)),
    }


def require_auth(request: Request, settings: AuthSettings, *, action: str = "sensitive action") -> dict[str, str]:
    if not settings.enabled:
        return {"actor": "local-unauthenticated", "auth_mode": "disabled"}
    if settings.local_demo_bypass:
        return {"actor": "local-demo-bypass", "auth_mode": "local_demo_bypass"}

    configured_plain = _split(settings.api_keys)
    configured_hashes = _split(settings.api_key_hashes)
    if not configured_plain and not configured_hashes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Auth is enabled but no API keys are configured for {action}",
        )

    token = _extract_token(request, settings.auth_header)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication required for {action}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if _token_matches(token, configured_plain, configured_hashes):
        return {"actor": _actor(request), "auth_mode": "api_key"}
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid Cascade API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_token(request: Request, header_name: str) -> str:
    value = request.headers.get(header_name) or request.headers.get("X-Cascade-Api-Key") or ""
    if header_name.lower() != "authorization" and value:
        return value.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def _token_matches(token: str, plain_keys: list[str], key_hashes: list[str]) -> bool:
    for key in plain_keys:
        if hmac.compare_digest(token, key):
            return True
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return any(hmac.compare_digest(digest, key_hash.lower()) for key_hash in key_hashes)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _actor(request: Request) -> str:
    explicit = request.headers.get("X-Cascade-Actor", "").strip()
    if explicit:
        return explicit[:128]
    return "authenticated-operator"

