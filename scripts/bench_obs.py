"""Admin-page cost at scale, and whether reading it stalls the gateway.

    uv run python scripts/bench_obs.py                 # N = 30,000 and 300,000
    uv run python scripts/bench_obs.py --n 30000 --chats 200

For each N: a temporary file database (WAL, as in production) is filled with calls made by the
real recording path (the simulator's normal-day, drift, provider-outage, shadow and budget
scenarios through the gateway), then that pool of real rows is copied in bulk up to N rows,
spread evenly over the last 23 hours and across four tenants. Everything runs in-process on
the FastAPI TestClient: no server, no network, no GPU.

Measured: GET /api/obs and GET /api/routing (median of --reps calls), then chat p50/p95 over
--chats sequential /v1/chat/completions requests, first alone and then while another thread
requests /api/obs back to back (does an admin refresh stall live traffic?).
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from llm_control_room.app import create_app  # noqa: E402
from llm_control_room.core import demo_key  # noqa: E402
from llm_control_room.gateway import Gateway  # noqa: E402

TENANTS = ("acme", "globex", "initech", "bench")
SCENARIOS = (
    ("normal-day", 1500),
    ("drift", 800),
    ("provider-outage", 600),
    ("shadow", 600),
    ("budget", 300),
)


def build(path: Path, n: int) -> tuple:
    app = create_app(str(path), admin_token="", allowed_hosts=None)
    core = app.state.core
    core.tenants.create("bench", budget_usd=1e9, rpm=10**9)
    core.tenants.add_key("bench", "bench", key=demo_key("bench"))
    for scenario, k in SCENARIOS:
        app.state.sim.run(scenario, n=k, hours=24.0, seed=7)
    store = core.store
    cols = [c for c in Gateway.COLS if c not in ("at", "tenant")]
    pool = store.all(f"SELECT {','.join(cols)} FROM calls ORDER BY id")
    store.run("DELETE FROM calls")
    end = time.time() - 600  # leave the last minutes free for the live chats
    span = 23 * 3600
    sql = (
        f"INSERT INTO calls(at, tenant, {','.join(cols)}) "
        f"VALUES(?, ?, {','.join('?' * len(cols))})"
    )
    batch: list[list] = []
    with store.transaction():
        for i in range(n):
            r = pool[i % len(pool)]
            batch.append([end - span + span * i / n, TENANTS[i % 4], *(r[c] for c in cols)])
            if len(batch) == 5000:
                store.con.executemany(sql, batch)
                batch = []
        if batch:
            store.con.executemany(sql, batch)
    return app, len(pool)


def timed(client: TestClient, path: str) -> float:
    t = time.perf_counter()
    r = client.get(path)
    ms = (time.perf_counter() - t) * 1000
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    return ms


def chats(client: TestClient, k: int, tag: str) -> list[float]:
    out = []
    head = {"Authorization": f"Bearer {demo_key('bench')}"}
    for i in range(k):
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": f"What is the capital of country {tag}{i}?"}],
        }
        t = time.perf_counter()
        r = client.post("/v1/chat/completions", json=body, headers=head)
        out.append((time.perf_counter() - t) * 1000)
        assert r.status_code == 200, r.text[:200]
    return out


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[max(0, min(len(s) - 1, -(-len(s) * p // 100) - 1))]


def verify(app, c: TestClient) -> str:
    """The page the endpoints return against the pre-rewrite row-at-a-time functions."""
    import json

    sys.path.insert(0, str(ROOT / "tests"))
    import obs_reference as ref

    store, models = app.state.core.store, app.state.core.providers.models()
    since = time.time() - 24 * 3600
    if store.one("SELECT COUNT(*) AS n FROM calls WHERE at>=?", (since,))["n"] > 200_000:
        return "n/a (the reference reads at most 200,000 rows)"
    got = c.get("/api/obs").json()
    got.pop("alerts")
    want = json.loads(json.dumps(ref.obs_page(store, since=since)))
    r = c.get("/api/routing").json()
    r.pop("baseline_model")
    want_r = json.loads(json.dumps(ref.routing_page(store, models, since=since)))
    return "identical" if (got, r) == (want, want_r) else "DIFFERENT"


def bench(n: int, reps: int, k: int, check: bool = False) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="lcr-bench-"))
    try:
        t = time.perf_counter()
        app, pool = build(tmp / "bench.db", n)
        built = time.perf_counter() - t
        c = TestClient(app, base_url="http://localhost")
        rows = app.state.core.store.one("SELECT COUNT(*) AS n FROM calls")["n"]
        timed(c, "/api/obs")  # warm
        obs_ms = statistics.median(timed(c, "/api/obs") for _ in range(reps))
        routing_ms = statistics.median(timed(c, "/api/routing") for _ in range(reps))
        same = verify(app, c) if check else "not checked"
        alone = chats(c, k, "a")

        stop, during, obs_done = threading.Event(), [], []

        def hammer():
            c2 = TestClient(app, base_url="http://localhost")
            while not stop.is_set():
                obs_done.append(timed(c2, "/api/obs"))

        th = threading.Thread(target=hammer)
        th.start()
        time.sleep(0.2)
        t = time.perf_counter()
        loaded = chats(c, k, "b")
        wall = time.perf_counter() - t
        stop.set()
        th.join()
        during = obs_done
        return {
            "n": rows,
            "pool": pool,
            "build_s": round(built, 1),
            "obs_ms": round(obs_ms, 1),
            "routing_ms": round(routing_ms, 1),
            "chat_p50": round(pct(alone, 50), 1),
            "chat_p95": round(pct(alone, 95), 1),
            "chat_p50_obs": round(pct(loaded, 50), 1),
            "chat_p95_obs": round(pct(loaded, 95), 1),
            "chat_max_obs": round(max(loaded), 1),
            "chat_rps_obs": round(k / wall, 1),
            "obs_during": len(during),
            "same": same,
        }
    finally:
        try:
            app.state.core.store.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="*", default=[30_000, 300_000])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--chats", type=int, default=100)
    ap.add_argument(
        "--verify", action="store_true", help="compare both pages with tests/obs_reference.py"
    )
    a = ap.parse_args()
    cols = (
        "n",
        "obs_ms",
        "routing_ms",
        "chat_p50",
        "chat_p95",
        "chat_p50_obs",
        "chat_p95_obs",
        "chat_max_obs",
        "chat_rps_obs",
        "obs_during",
    )
    print(" | ".join(cols))
    for n in a.n:
        r = bench(n, a.reps, a.chats, a.verify)
        print(" | ".join(str(r[c]) for c in cols), flush=True)
        print(
            f"  (pool of {r['pool']} real rows, built in {r['build_s']} s; "
            f"pages vs reference: {r['same']})",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
