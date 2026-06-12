# Releasing the desktop app (P4 M4.3)

Distribution is **direct download**, not the App Store: a signed, notarized DMG
on the download page. The pipeline is built and ready — the only thing standing
between it and a shippable build is an **Apple Developer ID** ($99/yr) and the
secrets below. Until those exist, every step still runs and produces an
*unsigned* `.app` you can test locally.

The app is the plan's **"daemon + browser UI"** model (§3 option D): the `.app`
launcher starts `sportsdata app` (gateway + conductor in one process) and opens
the chat UI in the browser. No Rust/Tauri toolchain is involved — a Tauri shell
is a later upgrade (§3 option A), not a blocker.

## The bundle, locally

```sh
pip install -e ".[build]"          # adds PyInstaller
sh scripts/build-desktop.sh        # dist/sportsdata/        (onedir: binary + runtime + data plane)
sh scripts/make-macos-app.sh       # dist/sportsdata.app     (launcher + Info.plist around the onedir)
open dist/sportsdata.app           # unsigned: right-click → Open the first time
```

For a real release the **sportsdata-mcp** data plane is bundled in:
`build-desktop.sh` looks for `$SPORTSDATA_MCP_DIR/.venv/bin/sportsdata-mcp`
(defaults to the sibling checkout) and embeds it; without it the app needs an
external `sportsdata-mcp` on PATH.

## What needs your accounts (one-time)

1. **Apple Developer Program** — enrol, then in Xcode/Developer portal create a
   **Developer ID Application** certificate. Export it as a `.p12` (with a
   password). Note your **Team ID**.
2. **Notarization credential** — either an **App Store Connect API key**
   (`.p8` + key id + issuer id), or an **app-specific password** for your Apple ID.

## Signing locally

```sh
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
# one of:
export APPLE_ID="you@example.com" APPLE_APP_PASSWORD="abcd-efgh-ijkl-mnop" APPLE_TEAM_ID="TEAMID"
#   or: APPLE_API_KEY_PATH=AuthKey_XXX.p8  APPLE_API_KEY_ID=XXX  APPLE_API_ISSUER=...

sh scripts/sign-and-notarize.sh    # → dist/sportsdata-<version>-macos.dmg (signed · notarized · stapled)
```

The script signs the bundle with the hardened runtime + `packaging/macos/entitlements.plist`
(the bundled CPython needs `allow-jit` / `disable-library-validation`), builds and
signs the DMG, submits to `notarytool --wait`, then staples the ticket so it
verifies offline. If notarization rejects an individual nested binary, sign the
Mach-O files inside `Resources/sportsdata/` first, then re-run (the `--deep` pass
covers them in practice).

## Releasing via CI

`.github/workflows/release.yml` runs on a `vX.Y.Z` tag (or manual dispatch). Add
these **repo secrets** first (Settings → Secrets and variables → Actions):

| secret | value |
|---|---|
| `APPLE_CERT_P12_BASE64` | `base64 -i DeveloperID.p12` |
| `APPLE_CERT_PASSWORD` | the `.p12` export password |
| `APPLE_SIGNING_IDENTITY` | `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_APP_PASSWORD`, `APPLE_TEAM_ID` | notarization (or the `APPLE_API_*` trio) |

Then:

```sh
git tag v0.19.0 && git push origin v0.19.0
```

The workflow builds the bundle + `.app`, imports the cert into a throwaway
keychain, signs/notarizes, and attaches the DMG to the release. **With no secrets
it uploads an unsigned `.app` zip instead** — so the workflow is green and useful
today; adding the secrets is what flips it to a signed DMG.

## Download page

Point the marketing site's "Download for macOS" button at the release asset
(`https://github.com/DanielTomaro13/sportsdata-agents/releases/latest`). Until the
first signed release, it links to the releases page.

## OTA data feed (optional, M4.5)

The market dictionary and capability labels can refresh **between** app releases —
no rebuild. Generate a data-signing keypair (same tool as the licence key:
`python scripts/license.py keygen`), bake the public half into builds as
`SPORTSDATA_DATA_PUBKEY`, and keep the private half as the `SPORTSDATA_DATA_PRIVKEY`
repo secret. The release workflow then publishes `data-bundle.json` as a release
asset automatically. Point clients at it:

```sh
export SPORTSDATA_DATA_FEED_URL="https://github.com/DanielTomaro13/sportsdata-agents/releases/latest/download/data-bundle.json"
agents update-data            # fetch → verify (offline) → apply overlay
agents update-data --check    # show the applied overlay version
```

To cut a bundle by hand: `SPORTSDATA_DATA_PRIVKEY=… python scripts/publish-data-bundle.py`.

## Later (not blockers)

- **Tauri shell** (§3 option A) — a native window/menubar instead of the browser,
  with the same sidecar. Pure upgrade; the daemon and UI are unchanged.
- **Homebrew cask** — `brew install --cask sportsdata` once a stable download URL
  exists.
- **Windows** (M4.5) — Authenticode signing + an MSI/NSIS installer; reuses the
  same `agents app` daemon.
