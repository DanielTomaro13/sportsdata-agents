# Updating the app

There are **three** independent update channels, smallest blast-radius first.

## 1. Data updates (OTA — no rebuild)

The market dictionary and capability labels are *data*. A running app refreshes
them without a new release:

```sh
agents update-data            # fetch → verify (offline) → apply as an overlay
agents update-data --check    # show the applied overlay version
```

The feed URL comes from `SPORTSDATA_DATA_FEED_URL` (default points at the latest
release asset). Bundles are Ed25519-signed and verified against the baked
`SPORTSDATA_DATA_PUBKEY`; a forged/unsigned bundle is refused. The overlay lands in
`<data_dir>/data-overlay/` and the loaders prefer it over the packaged seed.

To **publish** a bundle (operator): `SPORTSDATA_DATA_PRIVKEY=… python
scripts/publish-data-bundle.py` → upload `dist/data-bundle.json` as a release asset.
The release workflow does this automatically when the data key secret is set.

## 2. App updates (a new release)

The app itself updates by installing a newer signed build. The release pipeline is
tag-triggered:

```sh
git tag vX.Y.Z && git push origin vX.Y.Z
```

`.github/workflows/release.yml` builds the onedir bundle → wraps it as
`sportsdata.app` → signs + notarizes a DMG (when the Apple secrets are set) →
attaches it to the release. Full runbook: [../RELEASE.md](../RELEASE.md). An
in-app auto-updater (Sparkle) is a planned addition; until then a release is a
fresh download.

## 3. Version flow (for contributors)

- Bump `version` in `pyproject.toml` **and** `__version__` in `__init__.py`
  together (a test asserts they match).
- Every change ships as a branch → PR → CI (ruff whole-tree + mypy + `agents lint`
  + pytest + a Postgres integration job) → squash-merge. CI runs `ruff check .`
  (whole tree) — run that locally before pushing, not just `ruff check src tests`.

## Plan / tier changes (the user's "upgrade")

The user's *subscription* changes are a licence change, not an app update. Two ways,
both self-serve:

- **In the app** — click the tier chip (top-right of the chat UI) to open **Your
  plan**: it shows your entitlements, an **Upgrade plan** button (opens checkout),
  and a field to paste a key. Buy → paste the emailed key → instantly on the new tier.
- **CLI** —
  ```sh
  agents license               # show the current tier + entitlements
  agents license --activate <key>   # apply a new/upgraded licence (stored in the keychain)
  ```

A new licence takes effect immediately — the roster, MCP quota and gated features
re-resolve. The gateway backs this with `GET /account` and `POST /account/activate`
(localhost-only). `SPORTSDATA_UPGRADE_URL` sets where the Upgrade button points.
See [../PRICING.md](../PRICING.md).
