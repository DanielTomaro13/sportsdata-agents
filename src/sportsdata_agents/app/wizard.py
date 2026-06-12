"""First-run setup: pick a model provider, store the key in the OS keychain.

Deliberately tiny and dependency-light (rich prompts only) so it works the same
from the CLI today and behind a desktop UI later. The model-key resolution
order already prefers env → keychain → settings, so a key written here is
picked up everywhere with no further config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key_env: str  # the env/keychain name the runtime resolves
    label: str
    hint: str
    free_tier: bool = False


# Order mirrors the runtime's provider detection (config/models layer).
PROVIDERS: tuple[Provider, ...] = (
    Provider("ANTHROPIC_API_KEY", "Claude (Anthropic)", "console.anthropic.com → API keys"),
    Provider("OPENAI_API_KEY", "GPT (OpenAI)", "platform.openai.com → API keys"),
    Provider("GEMINI_API_KEY", "Gemini (Google)", "aistudio.google.com — has a FREE tier", free_tier=True),
    Provider("GROQ_API_KEY", "Groq", "console.groq.com — has a FREE tier", free_tier=True),
    Provider("OPENROUTER_API_KEY", "OpenRouter (many models)", "openrouter.ai → keys"),
)


def configured_provider() -> Provider | None:
    """The first provider that already has a key (env or keychain) — used to
    decide whether the wizard needs to run at all."""
    from sportsdata_agents.secrets import get_keychain_secret

    for provider in PROVIDERS:
        if os.environ.get(provider.key_env) or get_keychain_secret(provider.key_env):
            return provider
    return None


async def verify_key(provider: Provider, key: str) -> tuple[bool, str]:
    """A real, cheap call so a bad key fails at setup, not at first use."""
    os.environ[provider.key_env] = key  # the model gateway reads from env
    try:
        from sportsdata_agents.models.gateway import ModelGateway
        from sportsdata_agents.workspace import Workspace

        gateway = ModelGateway()
        reply = await gateway.complete(
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            tier="fast",
            workspace=Workspace(tenant_id="setup", workspace_id="setup"),
            max_tokens=8,
        )
        text = (getattr(reply, "text", "") or "").lower()
        return bool(text), text[:60] or "(empty reply)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def store_key(provider: Provider, key: str) -> str:
    """Persist the verified key. Keychain first; on a headless/no-keyring box,
    report that the caller should set the env var instead."""
    from sportsdata_agents.secrets import set_keychain_secret

    if set_keychain_secret(provider.key_env, key):
        return "keychain"
    return "env"  # caller falls back to .env guidance
