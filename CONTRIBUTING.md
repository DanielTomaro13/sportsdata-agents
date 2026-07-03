# Contributing

Contributions welcome — agents, quant tools, workbench UI, docs.

## Setup

```bash
# sibling data-plane checkout first (see README quickstart), then:
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Gates (CI runs exactly these)

```bash
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m pytest -m "not live and not eval"
# postgres integration job: pytest -m integration (SQLite works locally too)
```

## Ground rules

- **Advisory only** — no agent places bets or moves money; the deny-filter and
  spec validator enforce this and PRs weakening it won't merge.
- Grounding is non-negotiable: numbers must come from tool results.
- Offline tests by default; `live` marks anything hitting real endpoints.
- No secrets anywhere in the tree — env vars only.
