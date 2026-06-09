"""Secret references + resolution.

Specs and configs hold a **named reference** to a secret, never the value (§13). The
value is resolved at run time from the environment first, then a caller-supplied map
(per-workspace / settings secrets — a local-dev convenience). Resolved values are
wrapped in ``SecretStr`` so they don't leak into logs or reprs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, SecretStr


class MissingSecretError(RuntimeError):
    """A required secret was not found in the environment or the provided map."""

    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} is not set (checked the environment and workspace/settings secrets)")
        self.name = name


class SecretRef(BaseModel):
    """A named reference to a secret. Carry this in specs/configs — never the value."""

    name: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"SecretRef({self.name})"


def resolve_secret(ref: SecretRef | str, extra: Mapping[str, str] | None = None) -> SecretStr:
    """Resolve a secret by name: environment first, then ``extra``; else raise.

    Returns a ``SecretStr`` so the value is not accidentally printed.
    """
    name = ref.name if isinstance(ref, SecretRef) else ref
    value = os.environ.get(name)
    if value is None and extra is not None:
        value = extra.get(name)
    if value is None:
        raise MissingSecretError(name)
    return SecretStr(value)
