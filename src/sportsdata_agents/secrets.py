"""Secret references + resolution.

Specs and configs hold a **named reference** to a secret, never the value (§13). The
value is resolved at run time from the environment first, then the **OS keychain**
(the desktop app's secret store — never a plaintext file), then a caller-supplied
map (per-workspace / settings secrets — a local-dev convenience). Resolved values
are wrapped in ``SecretStr`` so they don't leak into logs or reprs.

The keychain tier is optional: ``keyring`` is an extra, and a server/CI deployment
with everything in the environment never reaches it. On a desktop install the
first-run wizard writes keys to the keychain via :func:`set_keychain_secret`, so a
user's API keys live encrypted in the OS store, not in a `.env` on disk.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "sportsdata"  # the keychain service namespace


class MissingSecretError(RuntimeError):
    """A required secret was not found in the environment, keychain, or the map."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"secret {name!r} is not set (checked the environment, the OS keychain, "
            "and workspace/settings secrets)"
        )
        self.name = name


class SecretRef(BaseModel):
    """A named reference to a secret. Carry this in specs/configs — never the value."""

    name: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"SecretRef({self.name})"


def _keyring() -> Any:
    """The keyring module, or None when the extra isn't installed (servers/CI)."""
    try:
        import keyring

        return keyring
    except Exception:  # pragma: no cover - import guard
        return None


def get_keychain_secret(name: str) -> str | None:
    """Read one secret from the OS keychain; None when absent or unavailable."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYCHAIN_SERVICE, name)
    except Exception as e:  # a locked/!unavailable keychain must degrade, not crash
        logger.warning("keychain read failed for %s: %s", name, e)
        return None


def set_keychain_secret(name: str, value: str) -> bool:
    """Store one secret in the OS keychain (the wizard's writer). False when the
    keyring extra is missing — the caller then falls back to .env guidance."""
    kr = _keyring()
    if kr is None:
        return False
    kr.set_password(KEYCHAIN_SERVICE, name, value)
    return True


def delete_keychain_secret(name: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYCHAIN_SERVICE, name)
        return True
    except Exception:
        return False


def resolve_secret(ref: SecretRef | str, extra: Mapping[str, str] | None = None) -> SecretStr:
    """Resolve a secret by name: environment first, then the OS keychain, then
    ``extra``; else raise. Returns a ``SecretStr`` so the value is not printed."""
    name = ref.name if isinstance(ref, SecretRef) else ref
    value = os.environ.get(name)
    if value is None:
        value = get_keychain_secret(name)
    if value is None and extra is not None:
        value = extra.get(name)
    if value is None:
        raise MissingSecretError(name)
    return SecretStr(value)
