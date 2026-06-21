#!/usr/bin/env python3
"""Single source of truth for the feed catalogue.

`site/catalogue.json` (provider id, name, kind, byo, proxied) is THE canonical list.
This script regenerates the derived copies so they can never silently drift:

  • services/entitlement/src/catalogue.ts  — PROVIDER_KIND + PROXIED_PROVIDERS (between
    the GENERATED markers; the grant/validation LOGIC around them is hand-written).

`site/feeds.html` already reads catalogue.json at runtime, so it has no generated copy.

Run `python scripts/gen-catalogue.py` after editing catalogue.json. The test
`tests/unit/test_catalogue_sync.py` runs it with --check and fails if anything drifted,
so the regeneration is enforced, not remembered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "site" / "catalogue.json"
WORKER_TS = ROOT / "services" / "entitlement" / "src" / "catalogue.ts"
START = "  // GENERATED from site/catalogue.json by scripts/gen-catalogue.py — do not edit by hand."
END = "  // END GENERATED"


def _providers() -> list[dict]:
    data = json.loads(CATALOGUE.read_text())
    provs = sorted(data["providers"], key=lambda p: p["id"])
    for p in provs:
        if p.get("kind") not in {"sport", "gambling", "social"}:
            raise SystemExit(f"catalogue: provider {p['id']!r} has invalid kind {p.get('kind')!r}")
    return provs


def _worker_block(provs: list[dict]) -> str:
    proxied = [p["id"] for p in provs if p.get("proxied")]
    kinds = ",\n".join(f'  {json.dumps(p["id"])}: {json.dumps(p["kind"])}' for p in provs)
    return (
        f"{START}\n"
        f"export const PROXIED_PROVIDERS = new Set<string>({json.dumps(proxied)});\n"
        f'export const PROVIDER_KIND: Record<string, "sport" | "gambling" | "social"> = {{\n'
        f"{kinds},\n"
        f"}};\n"
        f"{END}"
    )


def _splice(ts: str, block: str) -> str:
    i, j = ts.find(START), ts.find(END)
    if i == -1 or j == -1:
        raise SystemExit("catalogue.ts is missing the GENERATED markers")
    return ts[:i] + block + ts[j + len(END):]


def main() -> int:
    provs = _providers()
    block = _worker_block(provs)
    current = WORKER_TS.read_text()
    updated = _splice(current, block)
    check = "--check" in sys.argv
    if updated != current:
        if check:
            print("catalogue.ts is OUT OF SYNC with site/catalogue.json — run scripts/gen-catalogue.py", file=sys.stderr)
            return 1
        WORKER_TS.write_text(updated)
        print(f"regenerated {WORKER_TS.relative_to(ROOT)} ({len(provs)} providers)")
    else:
        print(f"catalogue.ts in sync ({len(provs)} providers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
