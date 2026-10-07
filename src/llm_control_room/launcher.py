"""``uv run llm-control-room``: start the server and open the browser."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .core import DEMO_TENANTS, demo_key
from .store import default_home

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def load_admin_token(home) -> str:
    """The admin token: LCR_ADMIN_TOKEN if set, else one generated on first run and kept in
    <data dir>/admin-token (so the browser tab the launcher opens is signed in, and nothing else
    on the machine can drive the control plane without reading that file)."""
    env = os.environ.get("LCR_ADMIN_TOKEN", "")
    if env:
        return env
    path = home / "admin-token"
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return tok


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket() as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError("no free port found")


def _open_when_ready(url: str, token: str = "") -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.15)
    # the fragment is never sent to the server; the page stores it and removes it from the address bar
    webbrowser.open(f"{url}/#token={token}" if token else url)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="llm-control-room", description="A self-hosted control plane for LLM apps."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--no-seed", action="store_true", help="do not simulate a first day of traffic")
    ap.add_argument("--db", help="SQLite file (default: ~/.llm-control-room/control-room.sqlite3)")
    a = ap.parse_args(argv)
    path = a.db or str(default_home() / "control-room.sqlite3")
    local = a.host in LOOPBACK
    token = load_admin_token(Path(path).parent if a.db else default_home())
    # Off the loopback interface there are no well-known demo keys, and any Host is answered
    # (the admin token and the Origin checks still apply).
    app = create_app(
        path,
        seed_tenants=local,
        admin_token=token,
        allowed_hosts="loopback" if local else None,
    )
    core, sim = app.state.core, app.state.sim
    if not local:
        for name in DEMO_TENANTS:
            for k in core.tenants.keys(name):
                if k["label"] == "demo key" and not k["revoked"]:
                    if core.tenants.authenticate(demo_key(name)):
                        core.tenants.revoke(k["id"])
    if not a.no_seed and local and core.store.one("SELECT COUNT(*) AS n FROM calls")["n"] == 0:
        print("First run: simulating a day of traffic through the mock provider...")
        sim.run("normal-day", n=900, hours=24, seed=1)
    port = free_port(a.port, a.host)
    url = f"http://{a.host}:{port}"
    print(f"LLM Control Room on {url}  (data: {path})  Ctrl+C to stop")
    print(
        "Admin API is protected by a token (LCR_ADMIN_TOKEN, or the admin-token file next to the data)."
    )
    if not a.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, token), daemon=True).start()
    uvicorn.run(app, host=a.host, port=port, log_level="warning")
