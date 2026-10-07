import time

import pytest
from conftest import ask

from llm_control_room import obs
from llm_control_room.providers import MOCK_MODELS
from llm_control_room.simulator import SCENARIOS, make_request


def rows_for(core, **kw):
    return obs.fetch(core.store, **kw)


def test_latency_percentiles_exclude_cache_hits(core):
    for i in range(30):
        ask(core, f"Explain how an index works ({i})")
    base = obs.summarise(rows_for(core))["p50_ms"]
    for _ in range(300):
        ask(core, "Explain how an index works (1)")  # 300 cache hits at 2 ms
    s = obs.summarise(rows_for(core))
    assert s["cache_hit_rate"] > 0.8 and s["p50_ms"] == base, (
        "cache hits must not move the percentiles"
    )


def test_cost_per_success_and_missing_grounding_is_not_zero(core):
    ask(core, "Hi there")
    ask(core, "How long do refunds take?", context="Refunds take 5 to 7 business days.")
    s = obs.summarise(rows_for(core))
    scored = [r["grounding"] for r in rows_for(core) if r["grounding"] is not None]
    assert s["calls"] == 2 and s["grounded_n"] == 1 and s["mean_grounding"] == scored[0]
    assert s["usd_per_success"] >= s["usd_per_call"]


def test_group_by_attributes_cost(core):
    ask(core, "Hi there", tenant="acme", feature="chat")
    ask(core, "Prove this step by step with trade-offs", tenant="initech", feature="analysis")
    g = {x["tenant"]: x for x in obs.group_by(rows_for(core), "tenant")}
    assert g["initech"]["usd"] > g["acme"]["usd"]
    assert {x["feature"] for x in obs.group_by(rows_for(core), "feature")} == {"chat", "analysis"}


def test_drift_is_stable_on_a_stable_day_and_flags_a_shift(app, sim):
    core = app.state.core
    sim.run("normal-day", n=800, seed=1)
    stable = obs.drift(rows_for(core))
    assert stable["ready"] and max(d["psi"] for d in stable["dimensions"]) < 0.25
    core.store.wipe_traffic()
    sim.run("drift", n=800, seed=1)
    shifted = obs.drift(rows_for(core))
    assert max(d["psi"] for d in shifted["dimensions"]) > 0.25
    assert shifted["worst"] in {d["dimension"] for d in shifted["dimensions"]}


def test_drift_needs_enough_calls(core):
    assert obs.drift(rows_for(core))["ready"] is False


def test_drift_reads_no_prompts(core):
    for i in range(60):
        ask(core, f"Explain how an index works ({i})")
    cols = set(rows_for(core)[0])
    assert not {"prompt", "text", "content", "messages"} & cols


def test_alerts_fire_once_then_respect_the_cooldown(app, sim):
    core = app.state.core
    sim.run("provider-outage", n=900, seed=2)
    assert core.alerts.list(), "an outage should raise at least one alert"
    n = len(core.alerts.list(500))
    core.alerts.evaluate()
    assert len(core.alerts.list(500)) == n


def test_alerts_need_a_minimum_sample(core):
    core.providers.mock.set_fault("nano-mock", error_rate=1.0)
    for i in range(10):
        try:
            ask(core, f"hi {i}")
        except Exception:
            pass
    assert core.alerts.evaluate() == []


def test_routing_report_and_frontier(app, sim):
    sim.run("normal-day", n=500, seed=1)
    rows = rows_for(app.state.core)
    rep = obs.routing_report(rows)
    assert rep["saved_usd"] > 0 and 0 <= rep["confusion"]["accuracy"] <= 1
    assert sum(sum(r) for r in rep["confusion"]["matrix"]) == rep["confusion"]["labelled"]
    f = obs.frontier(rows, list(MOCK_MODELS))
    names = {p["name"]: p for p in f["points"]}
    assert names["always nano-mock"]["usd"] < names["always titan-mock"]["usd"]
    assert names["router @ quality 0.75"]["usd"] < names["always titan-mock"]["usd"]
    assert (
        names["always titan-mock"]["expected_success"]
        > names["always nano-mock"]["expected_success"]
    )


def test_timeseries_buckets(app, sim):
    sim.run("normal-day", n=300, seed=1)
    series = obs.timeseries(rows_for(app.state.core), 12)
    assert len(series) == 12 and sum(p.get("calls", 0) for p in series) == 300


# ---------------------------------------------------------------- simulator


def test_same_seed_gives_the_same_traffic():
    import random

    a = [make_request(random.Random(5), "acme", "support-chat")["q"] for _ in range(1)]
    b = [make_request(random.Random(5), "acme", "support-chat")["q"] for _ in range(1)]
    assert a == b


def test_scenario_is_reproducible(app):
    sim, core = app.state.sim, app.state.core
    sim.run("normal-day", n=200, seed=4)
    first = [(r["model"], r["usd"]) for r in obs.fetch(core.store)]
    core.store.wipe_traffic()
    sim.run("normal-day", n=200, seed=4)
    assert first == [(r["model"], r["usd"]) for r in obs.fetch(core.store)]


def test_simulator_does_not_leave_the_clock_or_faults_changed(app):
    sim, core = app.state.sim, app.state.core
    sim.run("provider-outage", n=100, seed=1)
    assert not core.providers.mock.faults
    assert abs(core.clock() - time.time()) < 5


def test_every_scenario_runs(app):
    sim = app.state.sim
    for name in SCENARIOS:
        out = sim.run(name, n=60, seed=1)
        assert out["requests"] == 60 and out["new_calls"] >= 60


def test_budget_scenario_refuses_the_trial_tenant(app):
    out = app.state.sim.run("budget", n=80, seed=1)
    assert 0 < out["served"] < 80
    assert (
        app.state.core.store.one("SELECT COUNT(*) AS n FROM calls WHERE error_kind='budget'")["n"]
        > 0
    )


def test_unknown_scenario_and_bad_size(app):
    with pytest.raises(ValueError):
        app.state.sim.run("nope")
    with pytest.raises(ValueError):
        app.state.sim.run("normal-day", n=1)


def test_live_traffic_starts_and_stops(app):
    sim = app.state.sim
    assert sim.live(True, 50)["on"]
    time.sleep(0.5)
    assert sim.live(False)["sent"] > 0
    time.sleep(0.2)
    assert not sim.live_status()["on"]
