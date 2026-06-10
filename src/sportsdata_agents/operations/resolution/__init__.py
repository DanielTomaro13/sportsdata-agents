"""Resolution (post-P2 milestone): one fixture across every book's ids, one canonical
market name across every book's labels — the joins cross-book math depends on."""

from __future__ import annotations

from sportsdata_agents.operations.resolution.resolver import (
    cross_book_prices,
    resolve_events,
    split_sides,
)

__all__ = ["cross_book_prices", "resolve_events", "split_sides"]
