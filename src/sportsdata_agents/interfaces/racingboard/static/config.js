// Frontend runtime config. The live server ships this file as-is (auto-detect:
// connect the WebSocket; if it can't, fall back to replay data if present).
// The GitHub Pages build overwrites it to force replay mode. You can also point
// a static page at a deployed backend with ?api=wss://your-host/ws
window.MF_CONFIG = {
  forceReplay: false,     // Pages build sets true
  replayUrl: "data/replay.json",
  apiBase: null,          // e.g. "wss://racingboard.onrender.com" — else same origin
};
