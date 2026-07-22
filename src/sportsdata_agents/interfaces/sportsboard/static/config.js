// Runtime config. The warehouse-backed server ships this as-is (live). A static
// host (GitHub Pages) overrides it to forceReplay; you can also point a static
// page at a deployed backend with ?api=https://your-host
window.SB_CONFIG = { forceReplay: false, replayUrl: "data/replay.json", apiBase: null };
