"""Secret references + resolution.

Specs and configs hold a **named reference** to a secret, never the value (§13). The
value is resolved at run time in this order: the **environment**, then an
**app-private file** under the data dir (``secrets.json``, 0600), then the **OS
keychain**, then a caller-supplied map (per-workspace / settings secrets). Resolved
values are wrapped in ``SecretStr`` so they don't leak into logs or reprs.

Why the file BEFORE the keychain: an *unsigned* desktop app reading the keychain
triggers a macOS permission prompt (and the launcher could hang on it). The wizard
writes the key to the data-dir file (owner-only) AND best-effort to the keychain, so
the app reads its own key without ever prompting. The data dir is user-private; for a
single-user BYO-key desktop app that's the right trade. The keychain tier stays as a
fallback and ``keyring`` remains optional (servers/CI keep everything in the env).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)


def _write_private_atomic(path: Path, text: str) -> None:
    """Write `text` owner-only with NO world-readable window: a 0600 temp file (mkstemp
    creates 0600) written + fsync'd, then atomically renamed over the target. Avoids the
    `write_text()`-then-`chmod()` gap where the secret was briefly group/world-readable."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic; the result inherits the temp's 0600
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

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
    """Store one secret in the OS keychain (best-effort secondary store). False when
    the keyring extra is missing or the write fails — the file store is primary, so
    a keychain hiccup is non-fatal."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYCHAIN_SERVICE, name, value)
        return True
    except Exception as e:  # a locked/unavailable keychain must not crash setup
        logger.warning("keychain write failed for %s: %s", name, e)
        return False


def delete_keychain_secret(name: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYCHAIN_SERVICE, name)
        return True
    except Exception:
        return False


# ─── app-private file store (the desktop default) ──────────────────────────────
# An UNSIGNED desktop app reading the OS keychain triggers a macOS permission
# prompt (and the launcher could hang on it). So the wizard ALSO writes the key to
# an owner-only (0600) file in the app's private data dir, and resolution checks it
# BEFORE the keychain — the app reads its key without ever prompting. The data dir
# is user-private; for a single-user BYO-key desktop app this is the right trade.


def _secrets_file() -> Any:
    from sportsdata_agents.paths import data_dir

    return data_dir() / "secrets.json"


def _read_secrets_file() -> dict[str, str]:
    path = _secrets_file()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        pass
    return {}


def get_file_secret(name: str) -> str | None:
    """Read one secret from the app-private data-dir file (never prompts)."""
    v = _read_secrets_file().get(name)
    return str(v) if v is not None else None


def set_file_secret(name: str, value: str) -> bool:
    """Persist one secret to an owner-only (0600) file under the data dir."""
    try:
        path = _secrets_file()
        data = _read_secrets_file()
        data[name] = value
        _write_private_atomic(path, json.dumps(data, indent=2))
        return True
    except OSError as e:
        logger.warning("could not write the secrets file: %s", e)
        return False


def delete_file_secret(name: str) -> bool:
    try:
        data = _read_secrets_file()
        if name in data:
            del data[name]
            _write_private_atomic(_secrets_file(), json.dumps(data, indent=2))
        return True
    except OSError:
        return False


def resolve_secret(ref: SecretRef | str, extra: Mapping[str, str] | None = None) -> SecretStr:
    """Resolve a secret by name: environment first, then the OS keychain, then
    ``extra``; else raise. Returns a ``SecretStr`` so the value is not printed."""
    name = ref.name if isinstance(ref, SecretRef) else ref
    value = os.environ.get(name)
    if value is None:
        value = get_file_secret(name)  # app-private file first — never prompts
    if value is None:
        value = get_keychain_secret(name)
    if value is None and extra is not None:
        value = extra.get(name)
    if value is None:
        raise MissingSecretError(name)
    return SecretStr(value)
