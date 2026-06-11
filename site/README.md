# Marketing site (M3.4)

Static, framework-free (D21 allows Astro/Next; a static page ships first — drop-in
deployable on Vercel/Netlify/Pages as-is, swap to Astro when content grows).

- `index.html` — hero, live capability counters, the D22 hybrid demo (curated
  prompts → real `/demo/run` with tool calls shown; recorded playback from
  `demo-fallback.json` when the gateway is offline), persona cards, lead form.
- Point it at a deployed gateway by setting `window.GATEWAY_URL` (inline script
  tag or your host's env injection) — defaults to local `agents serve`.

Local preview:

```bash
.venv/bin/agents serve &              # gateway with /demo + /leads
python3 -m http.server -d site 8080   # open http://127.0.0.1:8080
```

Production notes: serve the gateway behind TLS, keep the demo's per-IP rate
limit, and set a real model key with a billing cap — the demo budget is
$0.10/run but the cap is your backstop.
