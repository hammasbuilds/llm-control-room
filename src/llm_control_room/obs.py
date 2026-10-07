"""Observability over the calls table: cost attribution, latency percentiles, grounding, drift,
alerts, and the routing reports.

Rules carried over from llm-observability-platform: percentiles and never means; latency
excludes cache hits; a call with no grounding score is not a call with a score of zero; drift
is PSI over prompt *shape* (length, question type) so no prompt is ever stored.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict

from ._vendor.psi import interpret, psi_categorical, psi_numeric
from .providers import ModelInfo
from .releases import percentile
from .store import Store

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
    by time. PSI per dimension, from prompt length, question type, length bucket and feature."""
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


# ------------------------------------------------------------------------------ alerts

RULES = [
    # name, field, absolute threshold, severity, relative multiplier on baseline, label
    ("error_rate", "error_rate", 0.05, "critical", 2.0, "error rate"),
    ("fallback_rate", "fallback_rate", 0.20, "warning", 3.0, "fallback rate"),
    ("ungrounded_rate", "ungrounded_rate", 0.20, "critical", 2.0, "ungrounded answer rate"),
    ("p95_latency", "p95_ms", 4000.0, "warning", 2.5, "p95 latency (ms)"),
]
MIN_SAMPLES = 30
COOLDOWN_S = 1800.0


class Alerts:
    def __init__(self, store: Store, clock=time.time) -> None:
        self.store, self.clock = store, clock

    def evaluate(self, window_calls: int = 60) -> list[dict]:
        """Per tenant, compare the newest ``window_calls`` calls with the ``window_calls`` before
        them. Windows are counted in calls rather than minutes so a quiet tenant is judged on the
        same amount of evidence as a busy one. Rules are relative to the baseline where one
        exists, and have a minimum sample count and a cooldown."""
        now = self.clock()
        fired = []
        for t in [r["name"] for r in self.store.all("SELECT name FROM tenants")]:
            rows = self.store.all(
                "SELECT * FROM calls WHERE tenant=? AND shadow=0 AND at<=? ORDER BY at DESC, id DESC "
                "LIMIT ?",
                (t, now, 2 * window_calls),
            )
            rows.reverse()
            cur = summarise(rows[-window_calls:])
            if cur.get("calls", 0) < MIN_SAMPLES:
                continue
            prev = summarise(rows[:-window_calls])
            for name, fld, thr, sev, mult, label in RULES:
                value = cur.get(fld)
                if value is None:
                    continue
                limit = thr
                base = prev.get(fld) if prev.get("calls", 0) >= MIN_SAMPLES else None
                if base:
                    limit = max(thr if fld != "p95_ms" else 1000.0, base * mult)
                if value <= limit:
                    continue
                last = self.store.one(
                    "SELECT at FROM alerts WHERE rule=? AND scope=? ORDER BY at DESC LIMIT 1",
                    (name, t),
                )
                if last and now - last["at"] < COOLDOWN_S:
                    continue
                msg = (
                    f"{label} {value:.2%} exceeds {limit:.2%}"
                    if fld != "p95_ms"
                    else f"{label} {value:.0f} exceeds {limit:.0f}"
                )
                self.store.run(
                    "INSERT INTO alerts(at, rule, severity, scope, message, value, threshold) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (now, name, sev, t, msg, value, limit),
                )
                fired.append({"rule": name, "severity": sev, "scope": t, "message": msg})
        return fired

    def list(self, limit: int = 50) -> list[dict]:
        return self.store.all("SELECT * FROM alerts ORDER BY at DESC, id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------------------ routing


def routing_report(rows: list[dict]) -> dict:
    served = [r for r in _served(rows) if r["provider"]]
    if not served:
        return {"calls": 0}
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
    served = [r for r in _served(rows) if r["provider"] and r["model"]]
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
        "note": "Mock models only; token counts as recorded; success = the mock's quality "
        "table at the true difficulty where known, else the estimate.",
    }
