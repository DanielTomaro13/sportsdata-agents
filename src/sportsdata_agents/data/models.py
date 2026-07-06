"""ORM models for the §9 data model (lean first cut; columns grow via migrations).

Tenant-owned tables inherit ``TenantScopedModel`` (UUID pk + tenant/workspace + created_at).
Public reference data (fixtures/events/selections) is global (inherits ``Base``) — the one
deliberate exception to tenant-scoping, since it's identical across tenants.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantScopedModel

# ─── Identity & tenancy ──────────────────────────────────────────────────


class Tenant(Base):
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")


class Workspace(Base):
    __tablename__ = "workspaces"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    provisioning: Mapped[str] = mapped_column(String(16), default="byo")  # byo | managed (§8.1)
    enabled_modules: Mapped[list] = mapped_column(JSON, default=list)
    mcp_groups: Mapped[list] = mapped_column(JSON, default=list)


class User(TenantScopedModel):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")


class Membership(TenantScopedModel):
    __tablename__ = "memberships"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")  # operator | member


# ─── Agents, conversations, audit ────────────────────────────────────────


class AgentSpec(TenantScopedModel):
    __tablename__ = "agent_specs"
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(32), default="0.1.0")  # semver (D27)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)


class Conversation(TenantScopedModel):
    __tablename__ = "conversations"
    channel: Mapped[str] = mapped_column(String(32), default="cli")  # cli | slack | discord | web
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Workbench chat management (M4.5): a user-set title (overrides the first-message
    # title) and an archive flag (hidden from the sidebar unless archived is shown).
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # Workbench B2 — per-conversation settings. ``model_tier``: a tier name or an
    # explicit "provider/model" forced for this chat (None = normal resolution).
    # ``mcp_providers``: the data providers this chat may reach (None = all licensed).
    # A UX scope, narrow-only: it can never widen past the licence or the B1 global
    # off-switch — those hard gates stay where they are.
    model_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mcp_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Message(TenantScopedModel):
    __tablename__ = "messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentRun(TenantScopedModel):
    __tablename__ = "agent_runs"
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    # Delegated sub-runs link to their caller (audit tree, §16). Plain Uuid, no FK:
    # self-referential FKs complicate cross-dialect batch ALTERs for no integrity gain here.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(128), index=True)
    # The task this run was asked to do (M4.5 observability) — the first user message,
    # stored so the activity list can show "what it was working on" without the transcript.
    input_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running | ok | error | paused
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCall(TenantScopedModel):
    __tablename__ = "tool_calls"
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool: Mapped[str] = mapped_column(String(128), index=True)
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RunTranscript(TenantScopedModel):
    """The distilled message transcript of one agent run (M4.5 observability): the
    task, the model's reasoning/narration between tool calls, and tool results — what
    the run actually did and "said to itself". One row per run; long contents are
    truncated. Lets the workbench show a run's trace and an agent's activity history."""

    __tablename__ = "run_transcripts"
    __table_args__ = (UniqueConstraint("agent_run_id", name="uq_transcript_run"),)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)


# ─── Cost / budgets / metrics (§16.1) ────────────────────────────────────


class UsageLedger(TenantScopedModel):
    __tablename__ = "usage_ledger"
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))  # llm | sandbox | tool
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[Decimal] = mapped_column(Numeric(16, 4), default=Decimal("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))


class Budget(TenantScopedModel):
    __tablename__ = "budgets"
    period: Mapped[str] = mapped_column(String(16), default="monthly")  # run | daily | monthly
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    resets_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMetric(TenantScopedModel):
    __tablename__ = "agent_metrics"
    agent: Mapped[str] = mapped_column(String(128), index=True)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    runs: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    cost_per_success_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    avg_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)


class Performance(TenantScopedModel):
    """Aggregated betting performance per window (M1.4; §9 deferred table)."""

    __tablename__ = "performance"
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    bets_settled: Mapped[int] = mapped_column(Integer, default=0)
    staked: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    pnl: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    roi: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    hit_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    avg_clv_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)


# ─── External memory (§8.2) ──────────────────────────────────────────────


class Memory(TenantScopedModel):
    __tablename__ = "memory"
    # remember() is select-then-write; this constraint is what actually guarantees
    # one row per key when writes race (migration 0004).
    __table_args__ = (UniqueConstraint("tenant_id", "workspace_id", "key", name="uq_memory_tenant_workspace_key"),)
    scope: Mapped[str] = mapped_column(String(24), default="workspace")  # user | workspace
    key: Mapped[str] = mapped_column(String(200), index=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Note(TenantScopedModel):
    __tablename__ = "notes"
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")


class Artifact(TenantScopedModel):
    __tablename__ = "artifacts"
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))  # chart | model | file | ...
    uri: Mapped[str] = mapped_column(String(1024))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


# ─── Recommendations vs tracked bets (distinct — §9) ─────────────────────


class Recommendation(TenantScopedModel):
    __tablename__ = "recommendations"
    selection: Mapped[str] = mapped_column(String(400))
    book: Mapped[str | None] = mapped_column(String(64), nullable=True)
    odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    stake_suggestion: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    edge: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    snapshot_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)  # provenance (§13.1)


class TrackedBet(TenantScopedModel):
    __tablename__ = "tracked_bets"
    selection: Mapped[str] = mapped_column(String(400))
    book: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    odds: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))
    placed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open → won | lost | void (cashed: future)
    result_pnl: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    closing_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)  # for CLV


# ─── Public reference data (GLOBAL — not tenant-scoped) ───────────────────


class Fixture(Base):
    __tablename__ = "fixtures"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sport: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)  # e.g. an MLB gamePk
    name: Mapped[str] = mapped_column(String(400), default="")
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # event END (e.g. exchange contracts key on resolution/expiry, not kickoff). Used as a
    # day-window proxy when start_time is unknown — it must NOT be mistaken for a real start
    # (the arb in-play gate reads start_time only, so an exchange fixture stays pre-game-unknown).
    end_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fixtures.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


# NOTE: there is no `Selection`/`selections` table. The intended fixture→event→selection
# hierarchy is only populated to fixture→event; selections live denormalized as strings on
# the warehouse rows (odds_snapshots/prices). The dead table was removed (migration 0014) so
# the schema describes what the system actually runs.

# ─── Odds-history warehouse (M2.1, §9.1 — GLOBAL: market data is tenant-neutral) ──


class OddsSnapshot(Base):
    """One raw odds observation. Append-only; prunable by retention (the time series
    of record lives in ``prices`` as change-points). Composite PK includes the time
    column so Timescale can hypertable it (partition column must be in the PK)."""

    __tablename__ = "odds_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # captured_at leads the composite time-window index below; the standalone
    # single-column indexes on provider/book/sport/market were pure write
    # amplification (~6 B-tree updates per insert at 17k rows/min) and served
    # none of the hot reads, which all filter provider+event+market+selection.
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True, index=True)
    provider: Mapped[str] = mapped_column(String(64))  # feed, e.g. nba_cdn
    book: Mapped[str] = mapped_column(String(64))  # bookmaker within the feed
    sport: Mapped[str] = mapped_column(String(32))
    event_external_id: Mapped[str] = mapped_column(String(128), index=True)
    event_name: Mapped[str] = mapped_column(String(400), default="")
    market: Mapped[str] = mapped_column(String(128))  # h2h | spread | total | ...
    selection: Mapped[str] = mapped_column(String(200))  # home | away -1.5 | over 220.5 ...
    odds: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    # advertised start, parsed from provider meta at write time — the resolver
    # windows fixtures on THIS, not capture day (futures are captured months out);
    # nullable: not every payload carries one
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # event END time when the payload carries one but no real start (exchanges key markets on
    # resolution/expiry). Feeds the resolver's day window; never treated as a kickoff.
    end_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        # the alert-context / latest-per-key lookup: filter the full key,
        # ORDER BY captured_at DESC LIMIT 1 — served in full by this composite
        Index("ix_snap_key_time", "provider", "event_external_id", "market",
              "selection", "captured_at"),
    )


class Price(Base):
    """Change-point series: a row only when a (feed, book, event, market, selection)
    price MOVES — line-movement queries and backtests read this, dedupe writes it."""

    __tablename__ = "prices"
    # One change-point per logical key + timestamp (migration 0013). The ingest path's
    # ON CONFLICT DO NOTHING keys on this, so a re-run / same-timestamp race is idempotent.
    # Includes changed_at so Timescale accepts it as a hypertable unique index.
    __table_args__ = (
        Index("uq_prices_change", "provider", "book", "event_external_id", "market",
              "selection", "changed_at", unique=True),
        # _load_latest_odds groups by (provider,book,event,market,selection) — an
        # EVENT-leading index bounds it to the batch's events' history instead of
        # walking ALL of a provider's change-points (betfair ~1.3M rows/day, never
        # pruned): this query runs inside the write gate every tick.
        Index("ix_prices_event_key", "event_external_id", "provider", "book",
              "market", "selection", "changed_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True, index=True)
    provider: Mapped[str] = mapped_column(String(64))
    book: Mapped[str] = mapped_column(String(64))
    sport: Mapped[str] = mapped_column(String(32))
    event_external_id: Mapped[str] = mapped_column(String(128), index=True)
    market: Mapped[str] = mapped_column(String(128))
    selection: Mapped[str] = mapped_column(String(200))
    odds: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    prev_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)  # None = first sighting


class RaceForm(Base):
    """Official form for one race (GLOBAL) — barriers, weights, jockeys and past
    performances from TAB's authenticated form guide. The racing ratings' REAL
    inputs (market-only signals can't see a wide barrier or a 3kg swing)."""

    __tablename__ = "race_form"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), index=True, default="tab")
    # "{date}:{raceType}:{venueMnemonic}:{raceNumber}" — TAB's own race identity
    race_key: Mapped[str] = mapped_column(String(128), index=True, unique=True)
    meeting_date: Mapped[str] = mapped_column(String(10))
    race_type: Mapped[str] = mapped_column(String(1))  # R / G / H
    venue_mnemonic: Mapped[str] = mapped_column(String(16))
    race_number: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # per-runner form dicts: number, name, barrier, weight, jockey/driver,
    # last starts, official ratings — trimmed from the form guide
    runners: Mapped[list] = mapped_column(JSON, default=list)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EventResult(Base):
    """Final result per event (GLOBAL) — what backtests settle against (M2.3)."""

    __tablename__ = "event_results"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    sport: Mapped[str] = mapped_column(String(32), index=True)
    event_external_id: Mapped[str] = mapped_column(String(128), index=True)
    winning_selection: Mapped[str] = mapped_column(String(200))  # e.g. "home"
    start_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


# ─── Models & predictions (M2.2 — tenant-scoped: your models are yours) ──────


class ModelArtifact(TenantScopedModel):
    """A trained model version + its calibration report (Brier/log-loss on holdout)."""

    __tablename__ = "models"
    name: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    sport: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(128), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # features/coefficients/notes
    calibration: Mapped[dict] = mapped_column(JSON, default=dict)  # {brier, log_loss, n_holdout}
    trained_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Prediction(TenantScopedModel):
    """One calibrated probability a model emitted for a selection (M2.2/M2.3)."""

    __tablename__ = "predictions"
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    event_external_id: Mapped[str] = mapped_column(String(128), index=True)
    market: Mapped[str] = mapped_column(String(128), default="")
    selection: Mapped[str] = mapped_column(String(200))
    prob: Mapped[Decimal] = mapped_column(Numeric(6, 5))  # calibrated, 0..1
    predicted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ─── Alerts & subscriptions (M3.2) + leads (M3.4) ────────────────────────────


class Subscription(TenantScopedModel):
    """A standing watch on the ingestion stream (M3.2): line moves, steam, value
    appearing/vanishing, scratchings. ``params`` carries the watch's filters and
    thresholds; ``cursor`` makes the watcher durable/resumable (§8.2)."""

    __tablename__ = "subscriptions"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), index=True)  # line_move|steam|value|scratching
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    channel: Mapped[str] = mapped_column(String(128), default="log")  # "log" | slack channel id
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    cursor: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(TenantScopedModel):
    """One fired alert (M3.2). ``dedupe_key`` stops the same condition refiring
    every cycle while it persists."""

    __tablename__ = "alerts"
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(String(1000))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(300), index=True)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)


class Lead(Base):
    """Marketing-site lead capture (M3.4) — pre-tenant, so not tenant-scoped."""

    __tablename__ = "leads"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), index=True)
    note: Mapped[str] = mapped_column(String(1000), default="")
    source: Mapped[str] = mapped_column(String(64), default="site")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
