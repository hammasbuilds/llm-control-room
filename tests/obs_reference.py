"""The row-at-a-time observability functions as they were before the SQL rewrite (commit
12a12fb), kept unchanged as the reference the SQL versions are compared with. They load every
call of the window into Python dicts; the product no longer does."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from llm_control_room._vendor.psi import interpret, psi_categorical, psi_numeric
from llm_control_room.providers import ModelInfo
from llm_control_room.releases import percentile
from llm_control_room.store import Store

LEVELS = ("easy", "medium", "hard")

def fetch(
    store: Store,
    *,
    since: float | None = None,
    until: float | None = None,
    tenant: str = "",
    feature: str = "",
    shadow: bool = False,
    limit: int = 200_000,
) -> list[dict]:
    q, a = "SELECT * FROM calls WHERE shadow=?", [int(shadow)]
    if since is not None:
        q += " AND at>=?"
        a.append(since)
    if until is not None:
        q += " AND at<=?"
        a.append(until)
    if tenant:
        q += " AND tenant=?"
        a.append(tenant)
    if feature:
        q += " AND feature=?"
        a.append(feature)
    q += " ORDER BY at LIMIT ?"
    a.append(limit)
    return store.all(q, a)


def _served(rows):
    return [r for r in rows if not r["error"]]


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"calls": 0}
    served = _served(rows)
    lat = [r["latency_ms"] for r in served if not r["cached"]]
    usd = sum(r["usd"] for r in rows)
    base = sum(r["baseline_usd"] for r in served)
    grounded = [r["grounding"] for r in rows if r["grounding"] is not None]
    known = [r["quality_ok"] for r in served if r["quality_ok"] is not None]
    ok_calls = [r for r in served if r["quality_ok"] != 0]
    blocked = sum(1 for r in rows if r["error_kind"] == "blocked")
    limited = sum(1 for r in rows if r["error_kind"] in ("rate_limited", "budget"))
    failed = sum(
        1
        for r in rows
        if r["error"] and r["error_kind"] not in ("blocked", "rate_limited", "budget")
    )
    return {
        "calls": n,
        "served": len(served),
        "blocked": blocked,
        "limited": limited,
        "failed": failed,
        "error_rate": round(failed / n, 4),
        "cache_hit_rate": round(sum(1 for r in rows if r["cached"]) / n, 4),
        "fallback_rate": round(sum(1 for r in rows if r["fallback_used"]) / n, 4),
        "redacted_rate": round(sum(1 for r in rows if json.loads(r["redactions"])) / n, 4),
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "p99_ms": percentile(lat, 99),
        "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows),
        "usd": round(usd, 6),
        "baseline_usd": round(base, 6),
        "saved_usd": round(base - sum(r["usd"] for r in served), 6),
        "usd_per_call": round(usd / n, 8),
        "usd_per_success": round(usd / len(ok_calls), 8) if ok_calls else None,
        "success_rate": round(sum(known) / len(known), 4) if known else None,
        "mean_grounding": round(sum(grounded) / len(grounded), 4) if grounded else None,
        "ungrounded_rate": round(sum(1 for g in grounded if g < 0.5) / len(grounded), 4)
        if grounded
        else None,
        "grounded_n": len(grounded),
    }


def group_by(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[str(r[key] or "-")].append(r)
    out = [{key: k, **summarise(v)} for k, v in buckets.items()]
    return sorted(out, key=lambda x: -x["usd"])


def add_shadow_spend(
    groups: list[dict], shadow_rows: list[dict], key: str, add_missing: bool = False
) -> list[dict]:
    """Shadow calls are never served, so they stay out of latency, success and per-call figures,
    but they are money the tenant's requests caused (the budget counts them). Each group gets
    ``shadow_usd`` and ``total_usd``; with ``add_missing`` a key seen only in shadow traffic (a
    model that only shadows) gets a row of its own, marked ``shadow_only``."""
    spend: dict[str, float] = defaultdict(float)
    rows_by: dict[str, list[dict]] = defaultdict(list)
    for r in shadow_rows:
        k = str(r[key] or "-")
        spend[k] += r["usd"]
        rows_by[k].append(r)
    have = set()
    for g in groups:
        k = str(g[key])
        have.add(k)
        g["shadow_usd"] = round(spend.get(k, 0.0), 6)
        g["total_usd"] = round(g.get("usd", 0.0) + g["shadow_usd"], 6)
    if add_missing:
        for k in sorted(set(rows_by) - have):
            s = summarise(rows_by[k])
            groups.append(
                {
                    key: k,
                    **s,
                    "usd": 0.0,
                    "shadow_usd": s["usd"],
                    "total_usd": s["usd"],
                    "shadow_only": True,
                }
            )
    return sorted(groups, key=lambda x: -x["total_usd"])


def timeseries(rows: list[dict], buckets: int = 24) -> list[dict]:
    if not rows:
        return []
    t0, t1 = rows[0]["at"], rows[-1]["at"]
    width = max((t1 - t0) / buckets, 1.0)
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        groups[min(buckets - 1, int((r["at"] - t0) / width))].append(r)
    out = []
    for i in range(buckets):
        s = summarise(groups.get(i, []))
        out.append({"t": t0 + i * width, **s})
    return out


# ------------------------------------------------------------------------------ drift


def _hist(values: list[float], edges: list[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        for i, e in enumerate(edges):
            if v < e:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    n = len(values) or 1
    return [round(c / n, 4) for c in counts]


def drift(rows: list[dict], current_fraction: float = 0.25) -> dict:
    """Reference = the earlier part of the window, current = the last ``current_fraction`` of it
    by time. PSI per dimension, from prompt length, question type, length bucket and feature.
    Rows without a prompt shape (a request blocked before its prompt was read) are left out: an
    unknown shape is not a zero-length prompt of type "other"."""
    rows = [r for r in rows if r["signature"]]
    if len(rows) < 40:
        return {"ready": False, "reason": "need at least 40 calls", "dimensions": []}
    t0, t1 = rows[0]["at"], rows[-1]["at"]
    cut = t1 - (t1 - t0) * current_fraction
    ref = [r for r in rows if r["at"] < cut]
    cur = [r for r in rows if r["at"] >= cut]
    if len(ref) < 20 or len(cur) < 20:
        return {
            "ready": False,
            "reason": "not enough calls on one side of the split",
            "dimensions": [],
        }

    def part(r, i):
        return (r["signature"].split(":") + ["", "", ""])[i]

    dims = []
    lens_ref, lens_cur = [r["prompt_len"] for r in ref], [r["prompt_len"] for r in cur]
    edges = sorted({sorted(lens_ref)[int(len(lens_ref) * q / 10)] for q in range(1, 10)})
    dims.append(
        {
            "dimension": "prompt length (chars)",
            "psi": psi_numeric(lens_ref, lens_cur),
            "labels": [f"<{int(e)}" for e in edges] + [f">={int(edges[-1])}" if edges else "all"],
            "reference": _hist(lens_ref, edges),
            "current": _hist(lens_cur, edges),
        }
    )
    for name, fn in (
        ("question type", lambda r: part(r, 0)),
        ("length bucket", lambda r: part(r, 1)),
        ("feature", lambda r: r["feature"]),
        ("tenant", lambda r: r["tenant"]),
    ):
        rv, cv = [fn(r) for r in ref], [fn(r) for r in cur]
        labels = sorted(set(rv) | set(cv))
        rc, cc = Counter(rv), Counter(cv)
        dims.append(
            {
                "dimension": name,
                "psi": psi_categorical(rv, cv),
                "labels": labels,
                "reference": [round(rc[k] / len(rv), 4) for k in labels],
                "current": [round(cc[k] / len(cv), 4) for k in labels],
            }
        )
    for d in dims:
        d["reading"] = interpret(d["psi"])
    return {
        "ready": True,
        "reference_n": len(ref),
        "current_n": len(cur),
        "split_at": cut,
        "dimensions": dims,
        "worst": max(dims, key=lambda d: d["psi"])["dimension"],
    }


# ------------------------------------------------------------------------------ routing


def routing_report(rows: list[dict]) -> dict:
    """Over the requests the router sent to a model. A cache hit reached no model: the cache
    saved that money, not the route, and the "always the baseline model" plan would have had the
    same hit, so counting it at the baseline's price would credit the router with the cache's
    savings. Cache hits are counted separately."""
    routed = [r for r in _served(rows) if r["provider"]]
    served = [r for r in routed if not r["cached"]]
    if not served:
        return {"calls": 0, "cache_hits": len(routed)}
    by_model = Counter(r["model"] for r in served)
    diff = Counter(LEVELS[r["diff_est"]] for r in served)
    spent = sum(r["usd"] for r in served)
    base = sum(r["baseline_usd"] for r in served)
    matrix = [[0, 0, 0] for _ in range(3)]
    for r in served:
        if r["diff_true"] is not None:
            matrix[r["diff_true"]][r["diff_est"]] += 1
    labelled = sum(sum(m) for m in matrix)
    correct = sum(matrix[i][i] for i in range(3))
    pinned = sum(1 for r in served if r["route_reason"] == "pinned by caller")
    return {
        "calls": len(served),
        "cache_hits": len(routed) - len(served),
        "by_model": dict(by_model),
        "by_difficulty": dict(diff),
        "usd": round(spent, 6),
        "baseline_usd": round(base, 6),
        "saved_usd": round(base - spent, 6),
        "saved_pct": round(100 * (base - spent) / base, 2) if base else 0.0,
        "pinned": pinned,
        "confusion": {
            "labels": list(LEVELS),
            "matrix": matrix,
            "labelled": labelled,
            "accuracy": round(correct / labelled, 4) if labelled else None,
            "note": "rows: true difficulty (simulator label), columns: router estimate",
        },
    }


def frontier(rows: list[dict], models: list[ModelInfo], baseline: str = "") -> dict:
    """What-if over recorded traffic: cost and expected success for 'always model X' and for the
    router at several quality thresholds. Uses recorded token counts and estimated difficulty
    (or the true label where the simulator supplied one), so nothing is re-run."""
    # cache hits cost nothing under every strategy, so they are left out of every point
    served = [r for r in _served(rows) if r["provider"] and r["model"] and not r["cached"]]
    if len(served) < 10:
        return {"ready": False, "points": []}
    mock = [m for m in models if m.mock] or models
    pts = []

    def level(r):
        return r["diff_true"] if r["diff_true"] is not None else r["diff_est"]

    for m in sorted(mock, key=lambda m: m.usd_out):
        cost = sum(m.cost(r["prompt_tokens"], r["completion_tokens"]) for r in served)
        q = sum(m.quality[level(r)] for r in served) / len(served)
        pts.append(
            {
                "name": f"always {m.id}",
                "kind": "fixed",
                "usd": round(cost, 6),
                "expected_success": round(q, 4),
            }
        )
    for mq in (0.7, 0.75, 0.8, 0.9, 0.95):
        cost = q = 0.0
        for r in served:
            ok = [m for m in mock if m.quality[r["diff_est"]] >= mq]
            m = (
                min(ok, key=lambda m: m.cost(r["prompt_tokens"], r["completion_tokens"]))
                if ok
                else max(mock, key=lambda m: m.quality[r["diff_est"]])
            )
            cost += m.cost(r["prompt_tokens"], r["completion_tokens"])
            q += m.quality[level(r)]
        pts.append(
            {
                "name": f"router @ quality {mq:g}",
                "kind": "router",
                "usd": round(cost, 6),
                "expected_success": round(q / len(served), 4),
            }
        )
    return {
        "ready": True,
        "n": len(served),
        "points": pts,
        "note": "Mock models only; calls that reached a model (cache hits excluded); token "
        "counts as recorded; success = the mock's quality table at the true difficulty where "
        "known, else the estimate.",
    }


# ------------------------------------------------------- the two endpoints as they composed it


def obs_page(store: Store, *, since=None, until=None, tenant: str = "", feature: str = "") -> dict:
    rows = fetch(store, since=since, until=until, tenant=tenant, feature=feature)
    sh = fetch(store, since=since, until=until, tenant=tenant, feature=feature, shadow=True)
    summary = summarise(rows)
    if summary.get("calls"):
        summary["shadow_usd"] = round(sum(r["usd"] for r in sh), 6)
        summary["total_usd"] = round(summary["usd"] + summary["shadow_usd"], 6)
    return {
        "summary": summary,
        "series": timeseries(rows, 24),
        "by_tenant": add_shadow_spend(group_by(rows, "tenant"), sh, "tenant"),
        "by_feature": add_shadow_spend(group_by(rows, "feature"), sh, "feature"),
        "by_model": add_shadow_spend(
            group_by([r for r in rows if r["model"]], "model"),
            [r for r in sh if r["model"]],
            "model",
            add_missing=True,
        ),
        "drift": drift(rows),
    }


def routing_page(store: Store, models, *, since=None, until=None) -> dict:
    rows = fetch(store, since=since, until=until)
    return {"report": routing_report(rows), "frontier": frontier(rows, models)}
