"""M4.5 — the workbench read surfaces: enriched /agents, /files, /settings, /mcp/groups.

Store-backed history (/conversations) is covered by the integration suite (needs a
warehouse); here we assert shapes and the graceful-empty contract with no store/data
plane, plus the desk-folder sandbox."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from sportsdata_agents.gateway.app import create_app

pytestmark = pytest.mark.unit


class FakeSession:
    agent_name = "orchestrator"
    recorder = None

    async def run(self, prompt: str, *, recorder: Any = None) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # an empty, isolated desk folder so /files is deterministic
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path / "desk"))
    app = create_app(session=FakeSession())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_agents_enriched(client: httpx.AsyncClient) -> None:
    agents = (await client.get("/agents")).json()
    assert "orchestrator" in agents
    a = agents["orchestrator"]
    # the workbench Agents view needs these fields
    for field in ("display_name", "description", "tier", "plane", "capabilities", "delegates_to", "skills"):
        assert field in a, f"missing {field}"
    assert isinstance(a["capabilities"], list)
    assert a["plane"] in ("product", "ops")


async def test_files_empty_desk(client: httpx.AsyncClient, tmp_path) -> None:
    body = (await client.get("/files")).json()
    assert body["files"] == []
    assert "desk" in body["desk_dir"]


async def test_files_lists_written_files(client: httpx.AsyncClient, tmp_path) -> None:
    desk = tmp_path / "desk"
    desk.mkdir(parents=True, exist_ok=True)
    (desk / "report.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (desk / ".hidden").write_text("x", encoding="utf-8")  # dotfiles excluded
    body = (await client.get("/files")).json()
    names = [f["name"] for f in body["files"]]
    assert "report.csv" in names
    assert ".hidden" not in names
    csv = next(f for f in body["files"] if f["name"] == "report.csv")
    assert csv["ext"] == "csv" and csv["size"] > 0


async def test_file_raw_serves_and_sandboxes(client: httpx.AsyncClient, tmp_path) -> None:
    desk = tmp_path / "desk"
    desk.mkdir(parents=True, exist_ok=True)
    (desk / "ok.txt").write_text("hello", encoding="utf-8")
    assert (await client.get("/files/raw", params={"name": "ok.txt"})).text == "hello"
    # path traversal must be rejected by resolve_desk_path
    esc = await client.get("/files/raw", params={"name": "../../etc/passwd"})
    assert esc.status_code == 400
    assert (await client.get("/files/raw", params={"name": "nope.txt"})).status_code == 404


async def test_settings_snapshot_shape(client: httpx.AsyncClient) -> None:
    s = (await client.get("/settings")).json()
    for field in ("provider", "model_key_configured", "data_dir", "warehouse", "desk_dir", "account"):
        assert field in s, f"missing {field}"
    assert isinstance(s["model_key_configured"], bool)
    assert isinstance(s["account"], dict) and "tier" in s["account"]


async def test_conversations_empty_without_store(client: httpx.AsyncClient) -> None:
    # no convstore injected (and FakeSession owns none) → graceful empty, never 500
    assert (await client.get("/conversations")).json() == {"conversations": []}
    assert (await client.get("/conversations/web-x/messages")).json() == {"messages": []}


class StubStore:
    """A minimal ConversationStore for endpoint-wiring tests (no DB)."""

    known: ClassVar[set[str]] = {"web-1"}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def list_conversations(self, *, include_archived: bool = False) -> list:
        return []

    async def messages_for(self, key: str):
        return None

    async def set_archived(self, key: str, archived: bool) -> bool:
        self.calls.append(("archive", key, archived))
        return key in self.known

    async def set_title(self, key: str, title: str) -> bool:
        self.calls.append(("rename", key, title))
        return key in self.known

    async def delete_conversation(self, key: str) -> bool:
        self.calls.append(("delete", key, None))
        return key in self.known


@pytest.fixture
async def store_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSDATA_AGENTS_DESK_DIR", str(tmp_path / "desk"))
    app = create_app(session=FakeSession(), conversation_store=StubStore())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_archive_rename_delete_wiring(store_client: httpx.AsyncClient) -> None:
    # archive
    assert (await store_client.post("/conversations/web-1/archive", json={"archived": True})).status_code == 200
    assert (await store_client.post("/conversations/nope/archive", json={"archived": True})).status_code == 404
    # rename
    assert (await store_client.post("/conversations/web-1/rename", json={"title": "Renamed"})).status_code == 200
    assert (await store_client.post("/conversations/web-1/rename", json={"title": "  "})).status_code == 422
    assert (await store_client.post("/conversations/nope/rename", json={"title": "x"})).status_code == 404
    # delete
    assert (await store_client.delete("/conversations/web-1")).status_code == 200
    assert (await store_client.delete("/conversations/nope")).status_code == 404


async def test_manage_endpoints_503_without_store(client: httpx.AsyncClient) -> None:
    # the default `client` fixture has no conversation store
    assert (await client.post("/conversations/web-1/archive", json={"archived": True})).status_code == 503
    assert (await client.delete("/conversations/web-1")).status_code == 503


async def test_mcp_groups_groups_by_provider(client: httpx.AsyncClient, monkeypatch) -> None:
    """The catalogue groups raw MCP groups under their provider. We stub the data
    plane so the unit test never spawns a subprocess."""
    import sportsdata_agents.mcp.manager as mgr

    class FakeManager:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> FakeManager:
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def call_tool(self, name: str, args: dict) -> dict:
            return {
                "available": {
                    "afl.core": {"provider": "afl", "tools": 20},
                    "afl.cfs": {"provider": "afl", "tools": 5},
                    "datagolf.general": {"provider": "datagolf", "tools": 12},
                },
                "providers": {
                    "afl": {"auth_env": [], "auth_required": False, "auth_optional": False},
                    "datagolf": {"auth_env": ["ZZ_DATAGOLF"], "auth_required": True, "auth_optional": False},
                },
            }

    monkeypatch.setattr(mgr, "MCPManager", FakeManager)
    monkeypatch.delenv("ZZ_DATAGOLF", raising=False)
    data = (await client.get("/mcp/groups")).json()
    provs = {p["provider"]: p for p in data["providers"]}
    assert set(provs) == {"afl", "datagolf"}
    assert provs["afl"]["tools"] == 25
    assert len(provs["afl"]["groups"]) == 2
    # status: open provider is ready; required-key provider with no key is needs_key
    assert provs["afl"]["status"] == "ready"
    assert provs["datagolf"]["status"] == "needs_key"
    assert provs["datagolf"]["auth_env"] == ["ZZ_DATAGOLF"]


def test_provider_status_classification(monkeypatch) -> None:
    from sportsdata_agents.gateway.app import _provider_status

    # no auth declared → ready
    assert _provider_status("espn", {})["status"] == "ready"
    # required + unconfigured → needs_key
    monkeypatch.delenv("ZZ_KEY", raising=False)
    s = _provider_status("x", {"auth_env": ["ZZ_KEY"], "auth_required": True})
    assert s["status"] == "needs_key" and s["key_configured"] is False
    # required + configured (env) → ready
    monkeypatch.setenv("ZZ_KEY", "set")
    s = _provider_status("x", {"auth_env": ["ZZ_KEY"], "auth_required": True})
    assert s["status"] == "ready" and s["key_configured"] is True
    # optional (kalshi) → ready even without a key
    monkeypatch.delenv("ZZ_OPT", raising=False)
    assert _provider_status("kalshi", {"auth_env": ["ZZ_OPT"], "auth_optional": True})["status"] == "ready"


async def test_coverage_endpoints_roundtrip(tmp_path, monkeypatch) -> None:
    """The Settings pane's coverage editor: read prefs, persist an edit, and
    the ingestion module sees the change immediately."""
    import httpx

    monkeypatch.setenv("SPORTSDATA_AGENTS_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.delenv("SPORTSDATA_AGENTS_COVERAGE", raising=False)
    from sportsdata_agents.gateway.app import create_app
    from sportsdata_agents.operations.ingestion import coverage as cov

    cov._prefs.cache_clear()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        r = await client.get("/coverage")
        assert r.status_code == 200
        assert r.json()["source"] == "default"
        assert "baseball" in r.json()["coverage"]
        r = await client.post("/coverage", json={"coverage": {
            "baseball": ["mlb"], "cricket": [], "Basketball": ["NBA", " wnba "]}})
        assert r.status_code == 200
        assert r.json()["source"] == "file"
        r = await client.get("/coverage")
        assert r.json()["coverage"] == {
            "baseball": ["mlb"], "basketball": ["nba", "wnba"], "cricket": []}
        # the ingestion gate reads the persisted prefs
        assert cov.competition_covered("Basketball", "WNBA")
        assert not cov.sport_covered("Golf")  # dropped by the edit
        # malformed shape → 422, nothing persisted
        r = await client.post("/coverage", json={"coverage": {"golf": "yes"}})
        assert r.status_code == 422
    cov._prefs.cache_clear()
