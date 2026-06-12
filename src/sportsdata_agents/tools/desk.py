"""Desk-folder export tools (P4 §4.2) — the "Cursor workspace" payoff.

Agents produce things the user wants to keep — a cross-book board, a backtest
table, a written brief. These write them as real files into the user-chosen
**desk folder** (``desk_dir`` / ``SPORTSDATA_AGENTS_DESK_DIR``), the only place
on disk an agent may write. Session-less and always available: a spec grants
them by name like any native tool.

Everything funnels through :func:`resolve_desk_path`, which rejects absolute
paths and ``..`` traversal — an agent can only ever write *inside* the desk
folder, never over the user's other files.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from sportsdata_agents.agents.harness import ToolDef
from sportsdata_agents.paths import resolve_desk_path

DESK_TOOL_NAMES = {"export_csv", "write_report"}


async def export_csv(args: dict[str, Any]) -> Any:
    """{filename, rows: [ {col: val} ], columns?} → write a CSV into the desk folder.

    Columns default to the union of keys across rows, in first-seen order (pass
    ``columns`` to fix the order / subset). Missing cells are blank; values are
    stringified. Returns the saved path the user can open.
    """
    filename = str(args.get("filename") or "").strip()
    rows = args.get("rows")
    if not filename:
        raise ValueError("filename is required")
    if not filename.lower().endswith(".csv"):
        filename += ".csv"
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list of objects")

    columns = args.get("columns")
    if columns:
        fieldnames = [str(c) for c in columns]
    else:  # union of keys, preserving first-seen order
        fieldnames = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(str(key))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames})
    data = buf.getvalue().encode("utf-8")

    path = resolve_desk_path(filename)
    path.write_bytes(data)
    return {"path": str(path), "rows": len(rows), "columns": fieldnames, "bytes": len(data)}


async def write_report(args: dict[str, Any]) -> Any:
    """{filename, content} → write a text/markdown report into the desk folder.

    For briefs, summaries and notes the user keeps. ``.md`` is appended when the
    filename has no extension. Returns the saved path.
    """
    filename = str(args.get("filename") or "").strip()
    content = args.get("content")
    if not filename:
        raise ValueError("filename is required")
    if content is None or not str(content):
        raise ValueError("content is required")
    if "." not in filename.rsplit("/", 1)[-1]:
        filename += ".md"

    data = str(content).encode("utf-8")
    path = resolve_desk_path(filename)
    path.write_bytes(data)
    return {"path": str(path), "bytes": len(data)}


DESK_TOOLS: dict[str, ToolDef] = {
    "export_csv": ToolDef(
        name="export_csv",
        description=(
            "Write tabular data (a board, a backtest, a comparison) as a CSV file into the "
            "user's desk folder so they can open it. Pass rows as a list of objects."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "e.g. 'afl-round12-board.csv' (subfolders ok)"},
                "rows": {
                    "type": "array",
                    "description": "Each object is a row; keys are columns",
                    "items": {"type": "object"},
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit column order/subset",
                },
            },
            "required": ["filename", "rows"],
        },
        execute=export_csv,
    ),
    "write_report": ToolDef(
        name="write_report",
        description=(
            "Save a written brief/summary as a text or markdown file in the user's desk "
            "folder. Use for reports the user wants to keep, not for chat answers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "e.g. 'nba-finals-brief.md'"},
                "content": {"type": "string", "description": "The report body (markdown or plain text)"},
            },
            "required": ["filename", "content"],
        },
        execute=write_report,
    ),
}
