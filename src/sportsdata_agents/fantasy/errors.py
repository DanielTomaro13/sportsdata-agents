"""Making an MCP failure legible.

The MCP client runs inside an anyio task group, so a clean upstream error — "fpl needs
an API key: set FPL_SESSION_COOKIE … (HTTP 403)" — reaches callers wrapped as
`ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`. `str(e)` on that
tells the reader nothing at all, and it has now bitten twice: once by classifying a
missing cookie as UNKNOWN (which never pages), and once by recording it as a proposal's
failure reason, where the owner sees a stack-plumbing detail instead of the one sentence
that says what to do.

Anything reporting or classifying an MCP failure should flatten it first.
"""

from __future__ import annotations

#: __context__ chains can cycle, so the walk is bounded rather than trusting the tree.
_MAX_DEPTH = 6


def flatten_error(e: BaseException, _depth: int = 0) -> str:
    """Every message in an exception tree, joined."""
    if _depth > _MAX_DEPTH:
        return ""
    parts = [str(e)]
    for sub in getattr(e, "exceptions", ()) or ():
        parts.append(flatten_error(sub, _depth + 1))
    for chained in (e.__cause__, e.__context__):
        if chained is not None and chained is not e:
            parts.append(flatten_error(chained, _depth + 1))
    return " | ".join(p for p in parts if p)


def best_message(e: BaseException, limit: int = 200) -> str:
    """The most USEFUL line in an exception tree, for a human reading a failure.

    Prefers the innermost message that actually says something over the task-group
    wrapper that says nothing.
    """
    parts = [p.strip() for p in flatten_error(e).split("|") if p.strip()]
    useful = [p for p in parts if "unhandled errors in a TaskGroup" not in p]
    return (useful[-1] if useful else (parts[0] if parts else repr(e)))[:limit]
