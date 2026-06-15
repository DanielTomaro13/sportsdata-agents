"""Native desktop window (the app in its own window, not a web browser).

Uses **pywebview** — a thin wrapper over the OS-native web view (WKWebView on
macOS, WebView2 on Windows, WebKitGTK on Linux). No Chromium bundle, no Rust:
the chat UI the gateway already serves is shown in a real application window.

Architecture: the gateway (+ conductor) runs in a background thread with its own
asyncio loop; the native window owns the main thread and the app's lifecycle —
closing the window sets a stop event that tears the daemon down. macOS requires
the GUI event loop (``webview.start()``) to run on the main thread, which is why
the server is the one pushed to a worker thread.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request

from sportsdata_agents.app.supervisor import DEFAULT_HOST, DEFAULT_PORT, run_app_async

logger = logging.getLogger(__name__)

WINDOW_TITLE = "sportsdata"


def _free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if it's bindable, else an OS-assigned free port — so a
    second window (or a stray daemon) never collides on the default."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _wait_healthz(base_url: str, timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def run_desktop(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    with_conductor: bool = True,
    title: str = WINDOW_TITLE,
) -> None:
    """Start the gateway in a background thread and show it in a native window.
    Blocks until the window is closed, then shuts the daemon down."""
    import webview  # imported here so the dep is only needed for window mode

    port = _free_port(host, port)
    base_url = f"http://{host}:{port}/"
    stop_evt = threading.Event()

    def _serve() -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_app_async(
                host=host, port=port, with_conductor=with_conductor,
                install_signals=False, external_stop=stop_evt,
            ))
        except Exception:
            logger.exception("gateway thread crashed")
        finally:
            loop.close()

    server = threading.Thread(target=_serve, name="gateway", daemon=True)
    server.start()

    if not _wait_healthz(base_url):
        logger.error("gateway did not answer health in time — opening the window anyway")

    # Cache-bust per launch so the web view can't reuse a stale page (an old build
    # or a cached operator chip) from a previous session. The gateway also sends
    # `Cache-Control: no-store`, but a fresh URL guarantees it across the upgrade.
    window_url = f"{base_url}?_={os.getpid()}"
    webview.create_window(title, window_url, width=1200, height=820, min_size=(900, 600))
    try:
        webview.start()  # blocks on the main thread until the window is closed
    finally:
        stop_evt.set()
        server.join(timeout=8)
