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
}


def get_native_tools(names: list[str]) -> list[ToolDef]:
    """Resolve native tool names; a spec granting an unknown one fails loudly."""
    missing = [n for n in names if n not in NATIVE_TOOLS]
    if missing:
        raise KeyError(f"unknown native tool(s) {missing}; registered: {sorted(NATIVE_TOOLS)}")
    return [NATIVE_TOOLS[n] for n in names]
