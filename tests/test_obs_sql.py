"""The SQL observability pages against the row-at-a-time functions they replaced.

``obs_reference`` is the pre-rewrite code, unchanged. Every comparison here is exact equality of
the whole page (dict order included, via JSON), over simulator traffic and over hand-made and
random rows that reach the corners: every error kind, cache hits, shadow-only models, missing
grounding, unknown quality, refused rows with no prompt shape, empty tenant/model names, tied
timestamps, zero-token calls and percentiles at every group size.
"""

import json
import random
import threading

import obs_reference as ref
import pytest
from conftest import ask

from llm_control_room import obs
from llm_control_room.core import Core
from llm_control_room.gateway import Gateway
from llm_control_room.providers import MOCK_MODELS, ModelInfo
from llm_control_room.simulator import Simulator
from llm_control_room.store import Store

T0 = 1_800_000_000.0
KINDS = ("", "", "", "", "blocked", "rate_limited", "budget", "upstream", "timeout")


def same(a, b):
    assert json.dumps(a) == json.dumps(b)


def check(store, models=MOCK_MODELS, **kw):
    models = list(models)
    same(ref.obs_page(store, **kw), obs.obs_page(store, **kw))
    rkw = {k: v for k, v in kw.items() if k in ("since", "until")}
    same(ref.routing_page(store, models, **rkw), obs.routing_page(store, models, **rkw))


def insert(store, **row):
    rec = {
        "trace": "t",
        "at": T0,
        "tenant": "acme",
        "feature": "faq",
        "model": "",
        "provider": "",
        "redactions": "[]",
        "attempts": "[]",
    }
    rec.update(row)
    cols = list(rec)
    store.run(
        f"INSERT INTO calls({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [rec[c] for c in cols],
    )


def random_rows(store, rng: random.Random, n: int, *, ties: bool = False) -> None:
    models = [m.id for m in MOCK_MODELS] + ["ghost-model"]
    with store.transaction():
        for i in range(n):
            kind = rng.choice(KINDS)
            refused = kind in ("blocked", "rate_limited", "budget")
            shaped = not (refused and rng.random() < 0.7)
            served = not kind
            model = "" if refused else rng.choice(models)
            shadow = int(rng.random() < 0.1)
            if shadow and rng.random() < 0.5:
                model = "shadow-only-model"
            at = T0 + (i // 3) * 60.0 if ties else T0 + i * rng.choice((1.0, 7.5, 60.0))
            sig = (
                f"{rng.choice(['factual', 'howto', 'code', 'analysis'])}:"
                f"{rng.choice(['s', 'm', 'l'])}:{rng.randrange(5)}"
                if shaped
                else ""
            )
            insert(
                store,
                at=at,
                tenant=rng.choice(["acme", "globex", "initech", ""]),
                feature=rng.choice(["faq", "chat", "analysis", "summarise"]),
                model=model,
                provider="" if (refused or rng.random() < 0.03) else "mock",
                route_reason=rng.choice(["auto", "auto", "pinned by caller"]),
                diff_est=rng.randrange(3),
                diff_true=rng.choice([None, 0, 1, 2]),
                prompt_tokens=0 if rng.random() < 0.05 else rng.randrange(1, 4000),
                completion_tokens=rng.randrange(0, 800),
                usd=0.0 if refused else rng.random() / 100,
                baseline_usd=0.0 if refused else rng.random() / 30,
                latency_ms=round(rng.choice([2.0, rng.uniform(50, 5000)]), 3),
                cached=int(served and rng.random() < 0.15),
                fallback_used=int(rng.random() < 0.05),
                error="" if served else f"{kind}: something",
                error_kind=kind,
                redactions=rng.choice(["[]", "[]", '["redacted:email"]']),
                grounding=rng.choice([None, None, rng.random()]),
                quality_ok=rng.choice([None, 0, 1, 1]) if served else None,
                prompt_len=rng.randrange(1, 3000) if shaped else 0,
                signature=sig,
                shadow=shadow,
            )


@pytest.fixture()
def mem():
    s = Store(":memory:")
    yield s
    s.close()


def test_empty_and_tiny_windows(mem):
    check(mem)
    for i in range(5):
        insert(mem, at=T0 + i, model="nano-mock", provider="mock", latency_ms=10.0 + i)
    check(mem)
    insert(mem, at=T0 + 10, error="blocked: x", error_kind="blocked")
    check(mem)


@pytest.mark.parametrize("seed", range(6))
def test_random_rows_match_the_reference(mem, seed):
    rng = random.Random(seed)
    random_rows(mem, rng, rng.choice([45, 300, 1500]), ties=seed % 2 == 1)
    check(mem)
    check(mem, tenant="acme")
    check(mem, tenant="")
    check(mem, feature="analysis")
    check(mem, tenant="globex", feature="chat")
    check(mem, since=T0 + 200, until=T0 + 20_000)


def test_percentiles_at_every_group_size(mem):
    rng = random.Random(3)
    for g in range(1, 130):
        for _ in range(g):
            insert(
                mem,
                at=T0 + rng.random() * 1000,
                tenant=f"t{g}",
                model="nano-mock",
                provider="mock",
                latency_ms=rng.choice([1.0, 2.0, 3.0, rng.uniform(0, 1000)]),
            )
    check(mem)


def test_simulated_traffic_matches_the_reference():
    core = Core(Store(":memory:"))
    core.seed_tenants()
    sim = Simulator(core)
    for scenario, n in (
        ("normal-day", 600),
        ("drift", 400),
        ("provider-outage", 400),
        ("budget", 120),
        ("canary-bad", 300),
        ("ab-test", 200),
        ("shadow", 300),  # last: a scenario that re-creates the release clears its calls
    ):
        sim.run(scenario, n=n, seed=11)
    for i in range(30):  # real cache hits and redactions through the gateway
        ask(core, f"Explain how webhooks retry ({i % 4})")
        ask(core, f"My address is a{i}@example.com, when will my refund arrive?")
    store = core.store
    assert store.one("SELECT COUNT(*) AS n FROM calls WHERE shadow=1")["n"] > 0
    assert store.one("SELECT COUNT(*) AS n FROM calls WHERE cached=1")["n"] > 0
    check(store)
    for t in ("acme", "globex", "initech"):
        check(store, tenant=t)
    check(store, feature="summarise")
    # real models next to the mocks: the frontier compares the mocks only
    real = ModelInfo("gpt-x", "openai", 2.5, 10.0, 500, 10, (0.97, 0.9, 0.8))
    check(store, models=[*MOCK_MODELS, real])
    check(store, models=[real, ModelInfo("other", "openai", 2.5, 4.0, 1, 1, (0.9, 0.8, 0.7))])


def test_file_database_reads_without_the_store_lock(tmp_path):
    """On a WAL file the page is read on its own connection: it completes while another
    thread holds the store's lock (the gateway recording a call), and equals the reference."""
    store = Store(tmp_path / "w.db")
    random_rows(store, random.Random(9), 800)
    check(store)
    done = {}

    def read():
        done["page"] = obs.obs_page(store)
        done["routing"] = obs.routing_page(store, list(MOCK_MODELS))

    with store.lock:
        t = threading.Thread(target=read)
        t.start()
        t.join(20)
        assert not t.is_alive(), "the admin page waited for the gateway's lock"
    same(done["page"], ref.obs_page(store))
    store.close()


def test_reader_sees_one_snapshot_and_leaves_nothing_behind(tmp_path):
    store = Store(tmp_path / "s.db")
    random_rows(store, random.Random(2), 100)
    with store.reader() as con:
        before = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        insert(store, at=T0 + 1e6)  # committed by the writer meanwhile
        assert con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == before
    with store.reader() as con:
        assert con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == before + 1
        obs.Window(con)
    with store.reader() as con:
        assert not con.execute("SELECT name FROM sqlite_temp_master").fetchall()
    store.close()


def test_recorded_calls_match_the_reference_through_the_gateway_columns(mem):
    """Rows written by the gateway's own INSERT (every column it records)."""
    rng = random.Random(4)
    cols = Gateway.COLS
    for i in range(200):
        insert(mem, at=T0 + i * 10, model=rng.choice(["nano-mock", "sage-mock"]),
               provider="mock", latency_ms=rng.uniform(10, 900), usd=rng.random() / 1000,
               baseline_usd=rng.random() / 100, prompt_len=rng.randrange(5, 500),
               signature=f"factual:{rng.choice('sml')}:x", quality_ok=rng.choice([0, 1]))
    assert set(cols) <= {r["name"] for r in mem.all("PRAGMA table_info(calls)")}
    check(mem)


def test_shadow_spend_is_a_running_total_like_before(mem):
    """These shadow costs add up to 0.0127965 exactly; a running total gives
    0.012796499999999999, which rounds to 0.012796, the compensated sum to 0.012797. The old
    page showed the running total's rounding, and so must the new one."""
    costs = [0.001081, 0.0012265, 0.0017515, 0.0020255, 0.00195, 0.0018485, 0.0029135000000000003]
    insert(mem, at=T0, model="nano-mock", provider="mock", usd=0.001)
    for i, usd in enumerate(costs):
        insert(mem, at=T0 + 1 + i, model="titan-mock", provider="mock", usd=usd, shadow=1)
    page = obs.obs_page(mem)
    assert page["by_tenant"][0]["shadow_usd"] == 0.012796
    assert page["summary"]["shadow_usd"] == 0.012797  # this one was a sum() before too
    check(mem)


def test_router_ties_and_dominated_models_match_the_reference(mem):
    """min(ok, key=cost) keeps the first of equal costs. A and B cost the same whenever a
    call has no completion tokens, every model costs nothing for a call with no tokens, and
    C is cheaper than both on long prompts: the SQL choice must break each tie the same way."""
    a = ModelInfo("a", "x", 1.0, 2.0, 1, 1, (0.99, 0.9, 0.8))
    b = ModelInfo("b", "x", 1.0, 1.0, 1, 1, (0.98, 0.9, 0.7))
    c = ModelInfo("c", "x", 2.0, 0.5, 1, 1, (0.97, 0.95, 0.75))
    rng = random.Random(8)
    for i in range(300):
        pt, ct = rng.choice([(0, 0), (rng.randrange(1, 900), 0), (0, rng.randrange(1, 900)),
                             (rng.randrange(1, 900), rng.randrange(1, 900))])
        insert(mem, at=T0 + i, model="a", provider="x", prompt_tokens=pt, completion_tokens=ct,
               diff_est=rng.randrange(3), diff_true=rng.choice([None, 0, 1, 2]))
    for models in ([a, b, c], [b, a, c], [c, b, a]):
        check(mem, models=models)


def test_a_database_from_the_previous_version_gets_the_indexes(tmp_path):
    import sqlite3

    from llm_control_room import store as store_mod

    old = store_mod.SCHEMA.replace(
        "calls_tenant_at_usd ON calls(tenant, at, usd)", "calls_tenant_at ON calls(tenant, at)"
    )
    old = old.replace("DROP INDEX IF EXISTS calls_tenant_at;", "")
    old = old.replace("CREATE INDEX IF NOT EXISTS calls_shadow_at ON calls(shadow, at);", "")
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(old)
    con.close()
    s = Store(path)
    names = {r["name"] for r in s.all("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"calls_tenant_at_usd", "calls_shadow_at"} <= names and "calls_tenant_at" not in names
    plan = " ".join(
        r["detail"]
        for r in s.all(
            "EXPLAIN QUERY PLAN SELECT COALESCE(SUM(usd),0) AS s FROM calls "
            "WHERE tenant=? AND at>? AND at<=?",
            ("acme", 0, 1),
        )
    )
    assert "COVERING INDEX calls_tenant_at_usd" in plan  # the gateway's budget check
    s.close()
