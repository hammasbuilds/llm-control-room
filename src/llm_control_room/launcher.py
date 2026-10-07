"""``uv run llm-control-room``: start the server and open the browser."""

from __future__ import annotations

import argparse
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .app import create_app
from .store import default_home


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket() as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError("no free port found")


def _open_when_ready(url: str) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.15)
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="llm-control-room", description="A self-hosted control plane for LLM apps."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--no-seed", action="store_true", help="do not simulate a first day of traffic")
    ap.add_argument("--db", help="SQLite file (default: ~/.llm-control-room/control-room.sqlite3)")
    a = ap.parse_args(argv)
    path = a.db or str(default_home() / "control-room.sqlite3")
    app = create_app(path)
    core, sim = app.state.core, app.state.sim
    if not a.no_seed and core.store.one("SELECT COUNT(*) AS n FROM calls")["n"] == 0:
        print("First run: simulating a day of traffic through the mock provider...")
        sim.run("normal-day", n=900, hours=24, seed=1)
    port = free_port(a.port, a.host)
    url = f"http://{a.host}:{port}"
    print(f"LLM Control Room on {url}  (data: {path})  Ctrl+C to stop")
    if not a.no_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(app, host=a.host, port=port, log_level="warning")
