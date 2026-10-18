"""Round 1 correctness fixes, each checked against an independent computation.

Every test here failed on the code before the fix (see scripts/round_check.py for the full
table of checks)."""

import time

from conftest import ask

from llm_control_room import obs, sandbox
from llm_control_room.guard import screen
from llm_control_room.releases import percentile


def nearest_rank(values, p):
    v = sorted(values)
    return v[max(1, -(-int(p * len(v)) // 100)) - 1]


# --------------------------------------------------------------------------- guardrails


def test_card_with_double_spaces_is_redacted():
    r = screen("Card 4111  1111  1111  1111 failed", redact_pii=True)
    assert "redacted:credit_card" in r.findings
    assert "4111111111111111" not in "".join(ch for ch in r.text if ch.isdigit())


# --------------------------------------------------------------------------- percentiles


def test_percentile_is_nearest_rank():
    for n in range(1, 120):
        vals = [float(i) for i in range(1, n + 1)]
        for p in (50, 95, 99):
            assert percentile(vals, p) == nearest_rank(vals, p), (n, p)
    assert percentile([float(i) for i in range(1, 31)], 95) == 29.0  # was 28 (round-half-even)


# --------------------------------------------------------------------------- router report


def test_routing_report_does_not_credit_cache_hits_to_the_router(core):
    for _ in range(5):
        ask(core, "Explain how webhooks retry on failure.")  # 1 call, 4 cache hits
    called = core.store.all("SELECT * FROM calls WHERE cached=0")
    rep = obs.routing_page(core.store, core.providers.models())["report"]
    assert rep["calls"] == 1 and rep["cache_hits"] == 4
    assert abs(rep["baseline_usd"] - round(called[0]["baseline_usd"], 6)) < 1e-9
    assert sum(rep["by_model"].values()) == 1
    assert sum(map(sum, rep["confusion"]["matrix"])) == rep["confusion"]["labelled"]


# --------------------------------------------------------------------------- drift


def test_refused_requests_keep_their_prompt_shape(core):
    core.tenants.create("burst", rpm=1)
    for i in range(3):
        try:
            ask(core, f"Explain webhook retries for order {i}", tenant="burst", use_cache=False)
        except Exception:
            pass
    refused = core.store.all(
        "SELECT prompt_len, signature FROM calls WHERE error_kind='rate_limited'"
    )
    assert len(refused) == 2
    assert all(r["prompt_len"] > 0 and r["signature"] for r in refused)


def test_drift_leaves_out_rows_without_a_prompt_shape(core):
    rows = [(float(i), 40, "factual:s:abc") for i in range(100)]
    # 40 rows in the current window with no shape (blocked before the prompt was read)
    rows += [(99.5 + i / 100, 0, "") for i in range(40)]
    for at, n, sig in rows:
        core.store.run(
            "INSERT INTO calls(trace, at, tenant, feature, prompt_len, signature) "
            "VALUES('t', ?, 'acme', 'faq', ?, ?)",
            (at, n, sig),
        )
    d = obs.obs_page(core.store)["drift"]
    assert d["reference_n"] + d["current_n"] == 100
    assert all(x["psi"] < 0.1 for x in d["dimensions"])


# --------------------------------------------------------------------------- agents


def _wait(core, rid, done=("completed", "budget_stopped", "failed", "denied", "cancelled")):
    end = time.time() + 30
    while time.time() < end:
        r = core.runner.get(rid)
        if r["status"] in done:
            core.runner.threads[rid].join(5)
            return core.runner.get(rid)
        time.sleep(0.02)
    raise AssertionError(core.runner.get(rid)["status"])


def _run_cost_parts(core, tenant, rid):
    gw = core.store.one("SELECT COALESCE(SUM(usd),0) s FROM calls WHERE tenant=?", (tenant,))["s"]
    tools = sum(
        e["data"].get("usd", 0)
        for e in core.runner.events(rid)
        if e["kind"] in ("tool_call", "tool_error")
    )
    return gw, tools


def test_run_cost_includes_the_composed_answer(core):
    core.tenants.create("acct", budget_usd=10)
    r = core.runner.start("acct", "research", wait=True)
    r = _wait(core, r["id"])
    assert r["status"] == "completed"
    gw, tools = _run_cost_parts(core, "acct", r["id"])
    assert abs(r["result"]["usd"] - round(gw + tools, 6)) < 2e-6


def test_denied_tool_is_not_charged(core):
    core.tenants.create("deny", budget_usd=10)
    r = core.runner.start("deny", "email")
    end = time.time() + 10
    while core.runner.get(r["id"])["status"] != "awaiting_approval" and time.time() < end:
        time.sleep(0.02)
    core.runner.decide(r["id"], False)
    r = _wait(core, r["id"])
    gw, tools = _run_cost_parts(core, "deny", r["id"])
    assert tools == 0 and abs(r["result"]["usd"] - round(gw, 6)) < 2e-6


def test_cost_limit_is_never_exceeded(core):
    for limit in (0.05, 0.1, 0.2, 0.33):
        name = f"lim{int(limit * 100)}"
        core.tenants.create(name, budget_usd=10)
        r = core.runner.start(name, "spendthrift", limits={"max_usd": limit}, wait=True)
        r = _wait(core, r["id"])
        assert r["result"]["limit"] == "cost" and r["result"]["usd"] <= limit, (limit, r["result"])
        # and it did not stop early: one more paid search would have crossed the limit
        assert r["result"]["usd"] + 0.04 > limit


# --------------------------------------------------------------------------- sandbox


def test_output_cap_is_exact():
    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 4096)\n"
    for cap in (1000, 20_000):
        r = sandbox.run_code(flood, "restricted", wall_seconds=5, output_bytes=cap)
        assert r["output_truncated"]
        assert len(r["stdout"].encode()) + len(r["stderr"].encode()) == cap


def test_output_exactly_at_the_cap_is_not_truncated():
    r = sandbox.run_code(
        "import sys\nsys.stdout.buffer.write(b'y' * 1000)", "restricted", output_bytes=1000
    )
    assert r["stdout"] == "y" * 1000 and not r["output_truncated"] and r["exit_code"] == 0


# --------------------------------------------------------------------------- spend shown vs spend enforced


def _shadow_setup(app):
    core = app.state.core
    app.state.sim.run("shadow", n=120, seed=3)
    return core


def test_tenant_card_spend_counts_shadow_calls_like_the_budget(client, app):
    core = _shadow_setup(app)
    assert core.store.one("SELECT COUNT(*) n FROM calls WHERE shadow=1")["n"] > 0
    card = {t["name"]: t["spent_usd"] for t in client.get("/api/tenants").json()}
    usage = client.get("/v1/usage", headers={"Authorization": "Bearer lcr-demo-globex"}).json()
    assert abs(card["globex"] - usage["spent_usd"]) < 1e-9


def test_observability_spend_includes_shadow_calls(client, app):
    core = _shadow_setup(app)
    o = client.get("/api/obs?hours=24").json()
    all_usd = core.store.one("SELECT SUM(usd) s FROM calls")["s"]
    live_usd = core.store.one("SELECT SUM(usd) s FROM calls WHERE shadow=0")["s"]
    assert abs(o["summary"]["usd"] - round(live_usd, 6)) < 1e-6
    assert abs(o["summary"]["total_usd"] - round(all_usd, 6)) < 1e-6
    g = {x["tenant"]: x for x in o["by_tenant"]}["globex"]
    assert abs(g["total_usd"] - round(all_usd, 6)) < 1e-6
    shadow_only = [m["model"] for m in o["by_model"] if m.get("shadow_only")]
    assert shadow_only == ["titan-mock"]


def test_budget_scenario_uses_the_same_budget_on_a_repeat(app):
    sim = app.state.sim
    sim.run("budget", n=60, seed=4)
    first = app.state.core.tenants.get("trial")["budget_usd"]
    sim.run("budget", n=60, seed=4)
    assert app.state.core.tenants.get("trial")["budget_usd"] == first
