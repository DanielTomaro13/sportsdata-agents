"""The desktop daemon: secrets keychain, wizard, conductor loop (M4.1)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_keychain_round_trips_through_a_fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """env wins over keychain; keychain wins over the extra map; absent → raise."""
    import sportsdata_agents.secrets as secrets

    store: dict[tuple[str, str], str] = {}

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, name: str) -> str | None:
            return store.get((service, name))

        @staticmethod
        def set_password(service: str, name: str, value: str) -> None:
            store[(service, name)] = value

    monkeypatch.setattr(secrets, "_keyring", lambda: FakeKeyring)
    # the app-private file store is consulted before the keychain; neutralise it so
    # a real secrets.json on the dev's machine can't shadow this keychain test.
    monkeypatch.setattr(secrets, "get_file_secret", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert secrets.set_keychain_secret("ANTHROPIC_API_KEY", "sk-kc") is True
    assert secrets.get_keychain_secret("ANTHROPIC_API_KEY") == "sk-kc"
    # keychain resolves when env is absent
    assert secrets.resolve_secret("ANTHROPIC_API_KEY").get_secret_value() == "sk-kc"
    # env overrides keychain
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert secrets.resolve_secret("ANTHROPIC_API_KEY").get_secret_value() == "sk-env"


def test_no_keyring_installed_degrades_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    import sportsdata_agents.secrets as secrets

    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert secrets.get_keychain_secret("X") is None
    assert secrets.set_keychain_secret("X", "y") is False  # caller falls back to env


def test_wizard_provider_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    from sportsdata_agents.app import wizard

    monkeypatch.setattr(wizard, "get_keychain_secret", lambda name: None, raising=False)
    import sportsdata_agents.secrets as secrets
    monkeypatch.setattr(secrets, "get_keychain_secret", lambda name: None)
    # also neutralise the file store (env → file → keychain), else a real
    # secrets.json on the dev machine makes the "nothing configured" case fail.
    monkeypatch.setattr(secrets, "get_file_secret", lambda name: None)
    for p in wizard.PROVIDERS:
        monkeypatch.delenv(p.key_env, raising=False)
    assert wizard.configured_provider() is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert wizard.configured_provider().key_env == "GROQ_API_KEY"


async def test_conductor_loop_ticks_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process loop calls run_tick and honours the stop event — no cron."""
    import sportsdata_agents.app.supervisor as sup

    ticks: list[Any] = []

    def fake_run_tick(**kwargs: Any) -> Any:
        ticks.append(kwargs)
        from sportsdata_agents.operations.scheduler import TickReport

        return TickReport(ran=["ingest"])

    async def fake_nearest(_sf: Any) -> float | None:
        return 300.0  # 5min out → pace floor of 120s (the <5min ladder rung)

    monkeypatch.setattr(sup, "run_tick", fake_run_tick)
    monkeypatch.setattr(sup, "seconds_to_nearest_start", fake_nearest)

    class _Eng:
        async def dispose(self) -> None:
            return

    monkeypatch.setattr(sup, "make_engine", lambda url: _Eng())
    monkeypatch.setattr(sup, "make_sessionmaker", lambda eng: None)

    stop = asyncio.Event()
    task = asyncio.create_task(sup._conductor_loop(stop, tick_seconds=0))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert ticks and ticks[0]["pace"] == 120  # proximity pacing wired into the loop


async def test_supervise_restarts_a_crashing_child_until_stop() -> None:
    """A child that crashes is restarted with backoff (so a transient gateway error
    doesn't take the whole app down); a clean stop ends the supervision."""
    import sportsdata_agents.app.supervisor as sup

    stop = asyncio.Event()
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")  # crash the first two runs
        stop.set()  # the third run is healthy and we ask to shut down

    await asyncio.wait_for(
        sup._supervise("t", flaky, stop, base_backoff=0.001, max_backoff=0.005), timeout=2
    )
    assert len(calls) == 3  # crashed twice → restarted twice → then stop
