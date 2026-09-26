"""MP-4C — token -> identity. A caller CANNOT choose its identity.

The request body carries NO user_id and NO tenant_id. Identity is resolved
SERVER-SIDE from the presented token, so a caller cannot impersonate another user
or tenant through a request field — there is no field to set.

Tokens are compared with `hmac.compare_digest` over the WHOLE registry without an
early exit. Tokens are never logged, never returned, and never included in an
error message.
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

#: Env-only (like the repository's RUN_IDEMPOTENCY_HMAC_KEY precedent): the token
#: map is NOT a Settings field, has NO default, and fails closed when unset.
TOKENS_ENV = "EXECUTOR_SERVICE_TOKENS"

#: Docker/Compose secret convention. A FILE secret is preferred for deployment
#: because the value never appears in `docker compose config`, in a container's
#: `Config.Env`, or in a process listing.
TOKENS_FILE_ENV = "EXECUTOR_SERVICE_TOKENS_FILE"
DEFAULT_TOKENS_FILE = "/run/secrets/executor_service_tokens"


@dataclass(frozen=True)
class Identity:
    principal: str
    user_id: int
    tenant_id: Optional[str] = None


class IdentityRegistry:
    """An immutable token->Identity map with constant-time lookup."""

    def __init__(self, tokens: Mapping[str, Mapping]) -> None:
        entries: Dict[str, Identity] = {}
        for token, record in dict(tokens or {}).items():
            if not isinstance(token, str) or not token or not isinstance(record, dict):
                continue
            uid = record.get("user_id")
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            tenant = record.get("tenant_id")
            if tenant is not None and not isinstance(tenant, str):
                continue
            entries[token] = Identity(
                principal=str(record.get("principal") or "unknown"),
                user_id=uid, tenant_id=tenant)
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, presented: Optional[str]) -> Optional[Identity]:
        """Identity for the presented token, or None. Never raises, never logs."""
        given = presented or ""
        found: Optional[Identity] = None
        for token, identity in self._entries.items():
            if hmac.compare_digest(given, token):
                found = identity
        return found


def _parse(raw: object) -> IdentityRegistry:
    if not raw or not str(raw).strip():
        return IdentityRegistry({})
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return IdentityRegistry({})
    if not isinstance(parsed, dict):
        return IdentityRegistry({})
    return IdentityRegistry(parsed)


def registry_from_env(env: Optional[Mapping] = None) -> IdentityRegistry:
    """Parse `EXECUTOR_SERVICE_TOKENS`. Unset/blank/malformed => EMPTY (fail closed)."""
    return _parse((env if env is not None else os.environ).get(TOKENS_ENV, ""))


def registry_from_file(path: str) -> IdentityRegistry:
    """Parse a secret FILE. Missing/unreadable/blank/malformed => EMPTY."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return IdentityRegistry({})
    return _parse(raw)


def load_registry(env: Optional[Mapping] = None) -> IdentityRegistry:
    """The deployment loader: secret FILE first, else the env var, else EMPTY.

    When the configured file EXISTS it is authoritative — there is no silent
    fallback to the environment, so a broken secret fails closed rather than
    quietly authenticating from a different source.
    """
    e = env if env is not None else os.environ
    path = e.get(TOKENS_FILE_ENV) or DEFAULT_TOKENS_FILE
    if path and os.path.exists(path):
        return registry_from_file(path)
    return registry_from_env(e)
