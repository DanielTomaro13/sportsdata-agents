"""Native (in-process) deterministic tools + the registry agent specs resolve against.

These do the math that matters (P8): the LLM narrates, these compute. Each is a
plain ``ToolDef``; specs grant them by name (`tools.native`). Unknown names fail
loudly at runtime build — a spec granted something that doesn't exist.
"""

from __future__ import annotations

from typing import Any

from sportsdata_agents.agents.harness import ToolDef


def _implied_probability(odds: float) -> float:
    if odds < 1.01:
        raise ValueError(f"decimal odds must be >= 1.01, got {odds}")
    return 1.0 / odds


async def implied_probability(args: dict[str, Any]) -> Any:
    """{odds: 2.50} -> {probability: 0.4}"""
    odds = float(args["odds"])
    return {"odds": odds, "probability": round(_implied_probability(odds), 6)}


async def vig_removal(args: dict[str, Any]) -> Any:
    """{prices: [{name, odds}, ...]} -> fair probabilities (normalised) + overround."""
    prices = args["prices"]
    if not isinstance(prices, list) or len(prices) < 2:
        raise ValueError("prices must be a list of at least two {name, odds} entries")
    implied = [(p.get("name", f"#{i}"), _implied_probability(float(p["odds"]))) for i, p in enumerate(prices)]
    total = sum(prob for _, prob in implied)
    return {
        "overround": round(total, 6),
        "vig_pct": round((total - 1.0) * 100, 4),
        "fair_probabilities": [{"name": name, "probability": round(prob / total, 6)} for name, prob in implied],
    }


async def best_price(args: dict[str, Any]) -> Any:
    """{prices: [{book, odds}, ...]} -> the best (highest decimal) price and its book."""
    prices = args["prices"]
    if not isinstance(prices, list) or not prices:
        raise ValueError("prices must be a non-empty list of {book, odds} entries")
    best = max(prices, key=lambda p: float(p["odds"]))
    return {"book": best.get("book", "?"), "odds": float(best["odds"])}


async def expected_value(args: dict[str, Any]) -> Any:
    """{probability, odds} -> EV per unit staked: p*odds - 1 (positive = value)."""
    p = float(args["probability"])
    odds = float(args["odds"])
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be in (0, 1), got {p}")
    if odds < 1.01:  # same floor as implied_probability — malformed odds, not a price
        raise ValueError(f"decimal odds must be >= 1.01, got {odds}")
    ev = p * odds - 1.0
    return {"probability": p, "odds": odds, "expected_value": round(ev, 6), "is_value": ev > 0}


async def kelly_fraction(args: dict[str, Any]) -> Any:
    """{probability, odds} -> the Kelly-optimal fraction of bankroll: (b*p - q) / b.

    Informational only (advisory, §14): a suggested sizing the USER may apply — named
    `kelly_fraction`, not "*_stake", deliberately: it computes a fraction, takes no
    action, and a money-verb name would (rightly) trip the no-money deny-filter.
    """
    p = float(args["probability"])
    odds = float(args["odds"])
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be in (0, 1), got {p}")
    b = odds - 1.0
    if b <= 0:
        raise ValueError(f"decimal odds must exceed 1.0, got {odds}")
    fraction = (b * p - (1.0 - p)) / b
    return {"probability": p, "odds": odds, "kelly_fraction": round(max(fraction, 0.0), 6)}


async def lookup_book_ids(args: dict[str, Any]) -> Any:
    """{query, book?} -> matching (name, id) pairs from the weekly-refreshed catalogue.

    Resolves ANY sport/competition/market id across bookmakers without burning tool
    calls on discovery endpoints (or model context on their firehose payloads). The
    catalogue is maintained by `agents refresh-books`; only matches enter context.
    """
    import json

    from sportsdata_agents.operations.refresh_books import catalogue_path

    query = str(args["query"]).strip().lower()
    if not query:
        raise ValueError("query must be non-empty")
    book_filter = str(args.get("book", "")).strip().lower() or None
    path = catalogue_path()
    if not path.is_file():
        raise FileNotFoundError("book catalogue missing — run `agents refresh-books` first")
    catalogue = json.loads(path.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    for book, record in catalogue.items():
        if book_filter and book.lower() != book_filter:
            continue
        hits = [
            {"name": name, "id": id_}
            for name, id_ in record.get("entries", [])
            if query in name.lower()
        ][:12]
        if hits:
            results[book] = {"fetched_at": record.get("fetched_at"), "matches": hits}
    if not results:
        return {"query": args["query"], "matches": {}, "note": "no catalogue entries matched — try a broader term"}
    return {"query": args["query"], "matches": results}


async def run_python(args: dict[str, Any]) -> Any:
    """{code, timeout_s?} -> run Python in the configured sandbox; artifacts saved locally.

    Only grantable to specs declaring ``sandbox: ephemeral`` (enforced at runtime
    build, §10). Network is off; stdout/stderr come back; files the code writes
    (charts, CSVs) are saved under ./artifacts/ and returned as paths.
    """
    import uuid
    from pathlib import Path as _Path

    from sportsdata_agents.sandboxes import get_sandbox

    code = str(args["code"])
    timeout_s = min(max(float(args.get("timeout_s", 60.0)), 1.0), 300.0)
    result = await get_sandbox().run(code, network_policy="none", timeout_s=timeout_s)
    saved: list[str] = []
    if result.artifacts:
        out_dir = _Path("artifacts")
        out_dir.mkdir(exist_ok=True)
        run_tag = uuid.uuid4().hex[:8]
        for name, content in result.artifacts.items():
            path = out_dir / f"{run_tag}-{name.replace('/', '-')}"  # flatten subdir artifacts
            path.write_bytes(content)
            saved.append(str(path))
    return {
        "ok": result.ok,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-2000:],
        "artifacts": saved,
    }


NATIVE_TOOLS: dict[str, ToolDef] = {
    "implied_probability": ToolDef(
        name="implied_probability",
        description="Convert decimal odds to implied probability.",
        parameters={
            "type": "object",
            "properties": {"odds": {"type": "number", "description": "Decimal odds, e.g. 2.50"}},
            "required": ["odds"],
        },
        execute=implied_probability,
    ),
    "vig_removal": ToolDef(
        name="vig_removal",
        description="Remove the bookmaker margin: normalise a market's implied probabilities to fair probabilities.",
        parameters={
            "type": "object",
            "properties": {
                "prices": {
                    "type": "array",
                    "description": "All selections in one market",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "odds": {"type": "number"}},
                        "required": ["odds"],
                    },
                }
            },
            "required": ["prices"],
        },
        execute=vig_removal,
    ),
    "best_price": ToolDef(
        name="best_price",
        description="Find the best (highest) decimal price for a selection across bookmakers.",
        parameters={
            "type": "object",
            "properties": {
                "prices": {
                    "type": "array",
                    "description": "The same selection priced at different books",
                    "items": {
                        "type": "object",
                        "properties": {"book": {"type": "string"}, "odds": {"type": "number"}},
                        "required": ["odds"],
                    },
                }
            },
            "required": ["prices"],
        },
        execute=best_price,
    ),
    "expected_value": ToolDef(
        name="expected_value",
        description="Expected value per unit for a price given a (fair) probability: p*odds - 1. Positive = value.",
        parameters={
            "type": "object",
            "properties": {
                "probability": {"type": "number", "description": "Fair win probability (0-1), e.g. from vig_removal"},
                "odds": {"type": "number", "description": "Decimal odds on offer"},
            },
            "required": ["probability", "odds"],
        },
        execute=expected_value,
    ),
    "lookup_book_ids": ToolDef(
        name="lookup_book_ids",
        description=(
            "Resolve bookmaker ids for any sport/competition/market by name "
            "(e.g. 'AFL', 'NBA', 'rugby') from the weekly-verified catalogue — use this "
            "instead of guessing ids or calling discovery endpoints."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name fragment to match, e.g. 'AFL' or 'NBA'"},
                "book": {"type": "string", "description": "Optional: restrict to one bookmaker"},
            },
            "required": ["query"],
        },
        execute=lookup_book_ids,
    ),
    "run_python": ToolDef(
        name="run_python",
        description=(
            "Run Python code in an isolated sandbox (pandas/matplotlib available; no network). "
            "print() your findings; files you save (e.g. chart.png) are returned as artifact paths."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Complete Python script to execute"},
                "timeout_s": {
                    "type": "number",
                    "description": "Wall-clock cap in seconds (default 60, max 300) for long computations",
                },
            },
            "required": ["code"],
        },
        execute=run_python,
    ),
    "kelly_fraction": ToolDef(
        name="kelly_fraction",
        description=(
            "Kelly-optimal fraction of bankroll for a price given a (fair) probability — "
            "informational sizing guidance only; the user decides and acts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "probability": {"type": "number", "description": "Fair win probability (0-1)"},
                "odds": {"type": "number", "description": "Decimal odds on offer"},
            },
            "required": ["probability", "odds"],
        },
        execute=kelly_fraction,
    ),
}


def get_native_tools(names: list[str]) -> list[ToolDef]:
    """Resolve native tool names; a spec granting an unknown one fails loudly."""
    missing = [n for n in names if n not in NATIVE_TOOLS]
    if missing:
        raise KeyError(f"unknown native tool(s) {missing}; registered: {sorted(NATIVE_TOOLS)}")
    return [NATIVE_TOOLS[n] for n in names]
