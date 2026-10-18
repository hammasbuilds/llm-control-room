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
from collections.abc import Iterator
from contextlib import contextmanager
from fractions import Fraction
from functools import reduce
from operator import add, getitem, itemgetter

from ._vendor.psi import interpret, psi
from .providers import ModelInfo
from .releases import percentile
from .store import Store

LEVELS = ("easy", "medium", "hard")


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


# ------------------------------------------------------------------------------ the window
#
# The admin pages are computed in SQL, on the store's read connection: on a file database
# (WAL) it never takes the store lock, so the gateway goes on recording calls while a page is
# built, and the whole page reads one snapshot. No call row becomes a Python dict.
#
# A GROUP BY costs SQLite a sort of every row of the window, so the page makes one: an
# aggregate over cells (shadow x tenant x feature x model x time bucket x served / cache hit /
# refusal kind) gives the summary, the series and every group_by table by adding cells up.
# Counts and token totals add up exactly. Money and grounding are floats, and a float total
# depends on how it is grouped, so each is summed in two parts (see _limbs) that add up across
# cells to the correctly rounded total, which is what the compensated sum() of the old code
# gave. Percentiles are the nearest-rank element of the latencies, fetched as one column with
# their cell. Figures the old code added up as a plain running total (the router's what-if,
# shadow spend per group) are added the same way, over the same calls in the same order, so
# they round the same; the routing figures read the routed calls once, in call order.

_S = 4294967296.0  # 2**32
_SERVED = "error = ''"
_PCTS = (50, 95, 99)
_CATS = ("question type", "length bucket", "feature", "tenant")
_PART0 = (
    "(CASE WHEN instr(signature, ':') = 0 THEN signature "
    "ELSE substr(signature, 1, instr(signature, ':') - 1) END)"
)
_REST = "substr(signature, instr(signature, ':') + 1)"
_PART1 = (
    f"(CASE WHEN instr(signature, ':') = 0 THEN '' WHEN instr({_REST}, ':') = 0 THEN {_REST} "
    f"ELSE substr({_REST}, 1, instr({_REST}, ':') - 1) END)"
)


def _limbs(x: str) -> list[str]:
    """SUM(x) in two parts, x = (h + lo) / 2**32: h an integer, so its sum is exact in any
    grouping; lo below 1, summed compensated, so a total over cells is off by at most about
    n * 2**-85, a few millionths of the last bit of any real total. Overflows only past
    $2**31 in one window."""
    a = f"(({x}) * {_S!r})"
    h = f"CAST({a} AS INTEGER)"
    return [f"COALESCE(SUM({h}), 0)", f"COALESCE(SUM({a} - {h}), 0.0)"]


def _exact(h: int, lo: float) -> float:
    """The correctly rounded value of (h + lo) / 2**32."""
    return float((Fraction(h) + Fraction(lo)) / 2**32)


# One row per cell. The served / cache-hit / refusal-kind flags are part of the cell, so the
# counts that summarise() takes over subsets (served, blocked, failed, latency sample...) are
# whole cells, and no per-row CASE is needed for them.
_KIND = (
    "(CASE error_kind WHEN 'blocked' THEN 1 WHEN 'rate_limited' THEN 2 WHEN 'budget' THEN 2 "
    "ELSE 0 END)"
)
_CELL_KEY = (
    "shadow, tenant, feature, model, {bucket} AS b, error = '' AS ok, cached <> 0 AS hit, "
    f"{_KIND} AS kind"
)
_CELL_AGG = [
    "COUNT(*)",
    "SUM(fallback_used <> 0)",
    "SUM(redactions <> '[]')",
    "SUM(quality_ok IS NOT NULL)",
    "COALESCE(SUM(quality_ok), 0)",
    "SUM(quality_ok IS NOT 0)",
    "COUNT(grounding)",
    "COALESCE(SUM(grounding < 0.5), 0)",
    "SUM(prompt_tokens + completion_tokens)",
    "MIN(at)",
    *_limbs("usd"),
    *_limbs("baseline_usd"),
    *_limbs("grounding"),
]
# the measures a cell total carries, in this order
(N, SERVED, BLOCKED, LIMITED, FAILED, CACHED, FALLBACK, REDACTED, LAT_N, TOKENS, KNOWN_N,
 KNOWN_SUM, OK_CALLS, GROUNDED_N, UNGROUNDED) = range(15)
USD, SERVED_USD, BASE, GROUNDING = 15, 17, 19, 21
_ZERO2 = (0, 0.0)


def _measures(ok: int, hit: int, kind: int, agg) -> list:
    n, fb, red, known_n, known_sum, ok_calls, gn, ung, toks = agg[:9]
    usd, base, grounding = agg[10:12], agg[12:14], agg[14:16]
    return [
        n,
        n if ok else 0,
        n if kind == 1 else 0,
        n if kind == 2 else 0,
        n if not ok and kind == 0 else 0,
        n if hit else 0,
        fb,
        red,
        n if ok and not hit else 0,
        toks,
        known_n if ok else 0,
        known_sum if ok else 0,
        ok_calls if ok else 0,
        gn,
        ung,
        *usd,
        *(usd if ok else _ZERO2),
        *(base if ok else _ZERO2),
        *grounding,
    ]


def _add(acc: list | None, row) -> list:
    return list(row) if acc is None else [a + b for a, b in zip(acc, row, strict=True)]


def _summary(a: list | None, pct: dict[int, float]) -> dict:
    """summarise(), from a cell total and the latencies at the nearest ranks."""
    if a is None or not a[N]:
        return {"calls": 0}
    n, served, gn, known_n, ok_calls = a[N], a[SERVED], a[GROUNDED_N], a[KNOWN_N], a[OK_CALLS]
    usd = _exact(*a[USD : USD + 2])
    # sum() over no rows is the integer 0, and the old figures kept that type
    base = _exact(*a[BASE : BASE + 2]) if served else 0
    served_usd = _exact(*a[SERVED_USD : SERVED_USD + 2]) if served else 0
    return {
        "calls": n,
        "served": served,
        "blocked": a[BLOCKED],
        "limited": a[LIMITED],
        "failed": a[FAILED],
        "error_rate": round(a[FAILED] / n, 4),
        "cache_hit_rate": round(a[CACHED] / n, 4),
        "fallback_rate": round(a[FALLBACK] / n, 4),
        "redacted_rate": round(a[REDACTED] / n, 4),
        "p50_ms": pct.get(50, 0.0),
        "p95_ms": pct.get(95, 0.0),
        "p99_ms": pct.get(99, 0.0),
        "tokens": a[TOKENS],
        "usd": round(usd, 6),
        "baseline_usd": round(base, 6),
        "saved_usd": round(base - served_usd, 6),
        "usd_per_call": round(usd / n, 8),
        "usd_per_success": round(usd / ok_calls, 8) if ok_calls else None,
        "success_rate": round(a[KNOWN_SUM] / known_n, 4) if known_n else None,
        "mean_grounding": round(_exact(*a[GROUNDING : GROUNDING + 2]) / gn, 4) if gn else None,
        "ungrounded_rate": round(a[UNGROUNDED] / gn, 4) if gn else None,
        "grounded_n": gn,
    }


def _nearest(values: list[float]) -> dict[int, float]:
    """releases.percentile's nearest rank, for the three percentiles at once."""
    if not values:
        return {}
    values.sort()
    n = len(values)
    return {p: round(values[max(0, min(n - 1, (p * n + 99) // 100 - 1))], 3) for p in _PCTS}


def _label(v) -> str:
    return str(v or "-")  # group_by's key


class Window:
    """The calls of one time window (with an optional tenant/feature filter), read through
    ``con`` (a read connection from ``window(store, ...)``)."""

    def __init__(
        self, con, *, since=None, until=None, tenant: str = "", feature: str = "", buckets=24
    ):
        self.con, self.buckets = con, buckets
        where, self.args = [], {}
        for col, op, val in (
            ("at", ">=", since),
            ("at", "<=", until),
            ("tenant", "=", tenant or None),
            ("feature", "=", feature or None),
        ):
            if val is not None:
                name = f"w{len(self.args)}"
                where.append(f"{col} {op} :{name}")
                self.args[name] = val
        self.where = " AND ".join(where) or "1"
        self._cells: dict | None = None
        self._lat_cache: dict | None = None
        self._spend: dict | None = None

    def _q(self, sql: str, args: dict | None = None, tuples: bool = False) -> list:
        cur = self.con.execute(sql, {**self.args, **(args or {})})
        if tuples:
            cur.row_factory = None
        return cur.fetchall()

    def _edge(self, cond: str, desc: bool = False):
        """The first (or last) ``at`` of the window's calls matching ``cond``: an index probe."""
        row = self._q(
            f"SELECT at FROM calls WHERE {self.where} AND {cond} "
            f"ORDER BY at {'DESC' if desc else ''} LIMIT 1"
        )
        return row[0][0] if row else None

    # ------------------------------------------------------------------ cells

    def cells(self) -> dict:
        """One pass over the window, grouped by cell; everything else adds cells up."""
        if self._cells is not None:
            return self._cells
        t0, t1 = self._edge("shadow = 0"), self._edge("shadow = 0", desc=True)
        width = max((t1 - t0) / self.buckets, 1.0) if t0 is not None else 1.0
        args = {"t0": t0 if t0 is not None else 0.0, "bw": width, "last": self.buckets - 1}
        bucket = "MAX(0, MIN(:last, CAST((at - :t0) / :bw AS INTEGER)))"
        cells: dict = {}
        for shadow, t, f, m, b, ok, hit, kind, *agg in self._q(
            f"SELECT {_CELL_KEY.format(bucket=bucket)}, {', '.join(_CELL_AGG)} FROM calls "
            f"WHERE {self.where} GROUP BY 1, 2, 3, 4, 5, 6, 7, 8",
            args,
            tuples=True,
        ):
            key = (shadow, t, f, m, b)
            prev = cells.get(key)
            meas = _measures(ok, hit, kind, agg)
            cells[key] = (agg[9], meas) if prev is None else (min(prev[0], agg[9]), _add(prev[1], meas))
        self._cells = {"t0": t0, "width": width, "bucket": bucket, "args": args, "cells": cells}
        return self._cells

    def _rollup(self, key: str | None, shadow: int) -> dict:
        """{group: (first at, measures)}; key None = one group, "bucket" = time buckets."""
        out: dict = {}
        for (sh, t, f, m, b), (first, meas) in self.cells()["cells"].items():
            if sh != shadow:
                continue
            if key == "model" and not m:
                continue  # group_by(model) only sees calls that named a model
            g = {None: "", "bucket": b, "tenant": t, "feature": f, "model": m}[key]
            if key not in (None, "bucket"):
                g = _label(g)
            prev = out.get(g)
            out[g] = (first, meas) if prev is None else (min(prev[0], first), _add(prev[1], meas))
        return out

    def _latencies(self, shadow: int = 0, cond: str = "") -> dict:
        """{(tenant, feature, model, bucket): [latency]} of the served calls that were not
        cache hits."""
        c = self.cells()
        out: dict = defaultdict(list)
        cur = self.con.execute(
            f"SELECT tenant, feature, model, {c['bucket']}, latency_ms FROM calls "
            f"WHERE {self.where} AND shadow = {int(shadow)} AND {_SERVED} AND cached = 0 {cond}",
            {**self.args, **c["args"]},
        )
        cur.row_factory = None
        for t, f, m, b, v in cur:
            out[t, f, m, b].append(v)
        for v in out.values():
            v.sort()  # a group's list is then sorted runs, which sort() merges cheaply
        return out

    def _lat(self) -> dict:
        if self._lat_cache is None:
            self._lat_cache = self._latencies()
        return self._lat_cache

    def _pcts(self, key: str | None, lat: dict) -> dict:
        groups: dict = defaultdict(list)
        for (t, f, m, b), vals in lat.items():
            if key == "model" and not m:
                continue
            g = {None: "", "bucket": b, "tenant": t, "feature": f, "model": m}[key]
            if key not in (None, "bucket"):
                g = _label(g)
            groups[g] += vals
        return {g: _nearest(v) for g, v in groups.items()}

    def _ordered(self, key: str, groups: dict) -> list:
        """Group keys in the order their first call was made: first ``at``, then id."""
        firsts = sorted((first, g) for g, (first, _) in groups.items())
        out, i = [], 0
        while i < len(firsts):
            j = i
            while j + 1 < len(firsts) and firsts[j + 1][0] == firsts[i][0]:
                j += 1
            tied = [g for _, g in firsts[i : j + 1]]
            if len(tied) > 1:  # same timestamp: the lower id came first
                ids = {}
                for g in tied:
                    # the "-" group also holds the calls that named no tenant or feature
                    cond = f"{key} = :g"
                    if g == "-" and key != "model":
                        cond = f"({key} = :g OR {key} = '')"
                    ids[g] = self._q(
                        f"SELECT MIN(id) FROM calls WHERE {self.where} AND shadow = 0 "
                        f"AND at = :a AND {cond}",
                        {"g": g, "a": firsts[i][0]},
                    )[0][0]
                tied.sort(key=lambda g: ids[g])
            out += tied
            i = j + 1
        return out

    # ------------------------------------------------------------------ summaries

    def summary(self) -> dict:
        g = self._rollup(None, 0).get("")
        return _summary(g[1] if g else None, self._pcts(None, self._lat()).get("", {}))

    def group_by(self, key: str) -> list[dict]:
        groups = self._rollup(key, 0)
        pcts = self._pcts(key, self._lat())
        out = [
            {key: g, **_summary(groups[g][1], pcts.get(g, {}))}
            for g in self._ordered(key, groups)
        ]
        return sorted(out, key=lambda x: -x["usd"])

    def with_shadow_spend(self, groups: list[dict], key: str, add_missing: bool = False):
        """Shadow calls are never served, so they stay out of latency, success and per-call
        figures, but they are money the tenant's requests caused (the budget counts them).
        Each group gets ``shadow_usd`` and ``total_usd``; with ``add_missing`` a key seen only
        in shadow traffic (a model that only shadows) gets a row of its own, ``shadow_only``."""
        cond = " AND model <> ''" if key == "model" else ""
        spend = self._shadow_spend()[key]
        have = set()
        for g in groups:
            k = str(g[key])
            have.add(k)
            g["shadow_usd"] = round(spend.get(k, 0.0), 6)
            g["total_usd"] = round(g.get("usd", 0.0) + g["shadow_usd"], 6)
        missing = sorted(set(spend) - have)
        if add_missing and missing:
            shadow_groups = self._rollup(key, 1)
            pcts = self._pcts(key, self._latencies(1, cond))
            for k in missing:
                s = _summary(shadow_groups[k][1], pcts.get(k, {}))
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

    def _shadow_spend(self) -> dict:
        """{key: {group: shadow usd}} for tenant, feature and model, in one pass. Each is a
        running total (spend[k] += usd) in call order, as the old code added it: a total on a
        rounding boundary rounds differently from a compensated sum."""
        if self._spend is None:
            spend: dict = {k: defaultdict(float) for k in ("tenant", "feature", "model")}
            cur = self.con.execute(
                f"SELECT tenant, feature, model, usd FROM calls WHERE {self.where} "
                "AND shadow = 1 ORDER BY at, id",
                self.args,
            )
            cur.row_factory = None
            st, sf, sm = spend["tenant"], spend["feature"], spend["model"]
            for t, f, m, usd in cur:
                st[t or "-"] += usd
                sf[f or "-"] += usd
                if m:
                    sm[m] += usd
            self._spend = spend
        return self._spend

    def shadow_usd(self) -> float:
        g = self._rollup(None, 1).get("")
        return round(_exact(*g[1][USD : USD + 2]), 6) if g else 0

    def timeseries(self) -> list[dict]:
        c = self.cells()
        if c["t0"] is None:
            return []
        groups = self._rollup("bucket", 0)
        pcts = self._pcts("bucket", self._lat())
        out = []
        for i in range(self.buckets):
            g = groups.get(i)
            s = _summary(g[1], pcts.get(i, {})) if g else {"calls": 0}
            out.append({"t": c["t0"] + i * c["width"], **s})
        return out

    # ------------------------------------------------------------------ drift

    def drift(self, current_fraction: float = 0.25) -> dict:
        """Reference = the earlier part of the window, current = the last ``current_fraction``
        of it by time. PSI per dimension, from prompt length, question type, length bucket and
        feature. Rows without a prompt shape (a request blocked before its prompt was read) are
        left out: an unknown shape is not a zero-length prompt of type "other"."""
        shaped = "shadow = 0 AND signature <> ''"
        t0, t1 = self._edge(shaped), self._edge(shaped, desc=True)
        cut = t1 - (t1 - t0) * current_fraction if t0 is not None else 0.0
        # shape counts per side of the split, counted as they stream (Counter over the
        # cursor counts in C; a GROUP BY would sort every row first)
        cur = self.con.execute(
            f"SELECT at >= :cut, prompt_len, {_PART0}, {_PART1}, tenant, feature "
            f"FROM calls WHERE {self.where} AND {shaped}",
            {**self.args, "cut": cut},
        )
        cur.row_factory = None
        rows = [(*k, c) for k, c in Counter(cur).items()]
        if sum(r[6] for r in rows) < 40:
            return {"ready": False, "reason": "need at least 40 calls", "dimensions": []}
        lens: dict[int, Counter] = {0: Counter(), 1: Counter()}
        cats = {name: (Counter(), Counter()) for name in _CATS}
        for side, plen, p0, p1, tenant, feature, c in rows:
            lens[side][plen] += c
            for name, v in zip(_CATS, (p0, p1, feature, tenant), strict=True):
                cats[name][side][v] += c
        n_ref, n_cur = sum(lens[0].values()), sum(lens[1].values())
        if n_ref < 20 or n_cur < 20:
            return {
                "ready": False,
                "reason": "not enough calls on one side of the split",
                "dimensions": [],
            }
        ref_sorted = sorted(lens[0].items())

        def nth(k):  # the k-th (0-based) value of the sorted reference sample
            for v, c in ref_sorted:
                if k < c:
                    return v
                k -= c
            raise IndexError(k)

        edges = sorted({nth(n_ref * q // 10) for q in range(1, 10)})

        def counts(hist):
            out = [0] * (len(edges) + 1)
            for v, c in hist.items():
                out[next((i for i, e in enumerate(edges) if v < e), len(edges))] += c
            return out

        rc_len, cc_len = counts(lens[0]), counts(lens[1])
        dims = [
            {
                "dimension": "prompt length (chars)",
                "psi": psi([c / n_ref for c in rc_len], [c / n_cur for c in cc_len]),
                "labels": [f"<{int(e)}" for e in edges]
                + [f">={int(edges[-1])}" if edges else "all"],
                "reference": [round(c / n_ref, 4) for c in rc_len],
                "current": [round(c / n_cur, 4) for c in cc_len],
            }
        ]
        for name in _CATS:
            rc, cc = cats[name]
            labels = sorted(set(rc) | set(cc))
            dims.append(
                {
                    "dimension": name,
                    "psi": psi([rc[k] / n_ref for k in labels], [cc[k] / n_cur for k in labels]),
                    "labels": labels,
                    "reference": [round(rc[k] / n_ref, 4) for k in labels],
                    "current": [round(cc[k] / n_cur, 4) for k in labels],
                }
            )
        for d in dims:
            d["reading"] = interpret(d["psi"])
        return {
            "ready": True,
            "reference_n": n_ref,
            "current_n": n_cur,
            "split_at": cut,
            "dimensions": dims,
            "worst": max(dims, key=lambda d: d["psi"])["dimension"],
        }

    # ------------------------------------------------------------------ routing
    #
    # Both routing figures read the routed calls once, in call order, as narrow tuples, and
    # add them up with the same operations the old code used (sum(), Counter, running
    # totals) over the same sequence, so every figure is the same float.

    def _routing_rows(self, models: list[ModelInfo]) -> dict:
        mock = [m for m in models if m.mock] or models
        args: dict = {}
        for j, m in enumerate(mock):
            args[f"i{j}"], args[f"o{j}"] = float(m.usd_in), float(m.usd_out)
            for lv in range(3):
                args[f"q{j}_{lv}"] = m.quality[lv]
        cost = [
            f"((prompt_tokens * :i{j} + completion_tokens * :o{j}) / 1000000.0)"
            for j in range(len(mock))
        ]
        level = "COALESCE(diff_true, diff_est)"

        def choice(mq: float) -> tuple:
            """Per difficulty estimate, the models that can be min(ok, key=cost): those meeting
            ``mq`` (or the best one when none does), less any that an earlier one in the list
            costs no more than at every token count (it would win or tie, and ties go first)."""
            per = []
            for est in range(3):
                ok = [j for j, m in enumerate(mock) if m.quality[est] >= mq]
                if not ok:
                    best = max(mock, key=lambda m, est=est: m.quality[est])
                    ok = [mock.index(best)]
                keep = [
                    j
                    for j in ok
                    if not any(
                        mock[k].usd_in <= mock[j].usd_in and mock[k].usd_out <= mock[j].usd_out
                        for k in ok
                        if k < j
                    )
                ]
                per.append(tuple(keep))
            return tuple(per)

        def picked(ok: tuple, expr) -> str:
            if len(ok) == 1:
                return expr(ok[0])
            whens = []
            for j in ok:
                conds = " AND ".join(f"{cost[j]} <= {cost[k]}" for k in ok if k != j)
                whens.append(f"WHEN {conds} THEN {expr(j)}")
            return f"(CASE {' '.join(whens)} END)"

        def quality_of(j: int) -> str:
            whens = " ".join(f"WHEN {lv} THEN :q{j}_{lv}" for lv in range(3))
            return f"(CASE {level} {whens} END)"

        def per_call(ch: tuple, expr) -> str:
            if ch[0] == ch[1] == ch[2]:
                return picked(ch[0], expr)
            return (
                f"(CASE diff_est WHEN 0 THEN {picked(ch[0], expr)} "
                f"WHEN 1 THEN {picked(ch[1], expr)} ELSE {picked(ch[2], expr)} END)"
            )

        thresholds = (0.7, 0.75, 0.8, 0.9, 0.95)
        plans = [choice(mq) for mq in thresholds] if mock else []
        distinct = list(dict.fromkeys(plans))
        cols = [
            "model",
            "diff_est",
            "diff_true",
            "cached <> 0",
            "route_reason = 'pinned by caller'",
            "usd",
            "baseline_usd",
            f"diff_est * 3 + {level}",
            *cost,
        ]
        # a plan that names one model per difficulty estimate is read off the cost columns;
        # one that depends on the call's token counts is chosen per call in SQL
        extra = {}
        for ch in distinct:
            if any(len(ok) > 1 for ok in ch):
                extra[ch] = len(cols)
                cols += [per_call(ch, lambda j: cost[j]), per_call(ch, quality_of)]
        cur = self.con.execute(
            f"SELECT {', '.join(cols)} FROM calls WHERE {self.where} AND shadow = 0 "
            f"AND {_SERVED} AND provider <> '' ORDER BY at, id",
            {**self.args, **args},
        )
        cur.row_factory = None
        return {
            "rows": cur.fetchall(),
            "mock": mock,
            "thresholds": thresholds,
            "plans": plans,
            "extra": extra,
        }

    def routing_report(self, rr: dict | None = None) -> dict:
        """Over the requests the router sent to a model. A cache hit reached no model: the
        cache saved that money, not the route, and the "always the baseline model" plan would
        have had the same hit, so counting it at the baseline's price would credit the router
        with the cache's savings. Cache hits are counted separately."""
        routed = (rr or self._routing_rows([]))["rows"]
        served = [r for r in routed if not r[3]]
        if not served:
            return {"calls": 0, "cache_hits": len(routed)}
        by_model = Counter(map(itemgetter(0), served))
        diff = Counter(LEVELS[d] for d in map(itemgetter(1), served))
        spent = sum(map(itemgetter(5), served))
        base = sum(map(itemgetter(6), served))
        matrix = [[0, 0, 0] for _ in range(3)]
        pairs = zip(map(itemgetter(2), served), map(itemgetter(1), served), strict=True)
        for (t, e), c in Counter(pairs).items():
            if t is not None:
                matrix[t][e] += c
        labelled = sum(sum(m) for m in matrix)
        correct = sum(matrix[i][i] for i in range(3))
        return {
            "calls": len(served),
            "cache_hits": len(routed) - len(served),
            "by_model": dict(by_model),
            "by_difficulty": dict(diff),
            "usd": round(spent, 6),
            "baseline_usd": round(base, 6),
            "saved_usd": round(base - spent, 6),
            "saved_pct": round(100 * (base - spent) / base, 2) if base else 0.0,
            "pinned": sum(map(itemgetter(4), served)),
            "confusion": {
                "labels": list(LEVELS),
                "matrix": matrix,
                "labelled": labelled,
                "accuracy": round(correct / labelled, 4) if labelled else None,
                "note": "rows: true difficulty (simulator label), columns: router estimate",
            },
        }

    def frontier(self, models: list[ModelInfo], rr: dict | None = None) -> dict:
        """What-if over recorded traffic: cost and expected success for 'always model X' and
        for the router at several quality thresholds. Uses recorded token counts and estimated
        difficulty (or the true label where the simulator supplied one), so nothing is
        re-run. The router's per-call choice (the cheapest model meeting the threshold at the
        call's own token counts) is made in SQL."""
        rr = rr or self._routing_rows(models)
        # cache hits cost nothing under every strategy, so they are left out of every point
        served = [r for r in rr["rows"] if not r[3] and r[0]]
        if len(served) < 10:
            return {"ready": False, "points": []}
        mock, n = rr["mock"], len(served)
        codes = list(map(itemgetter(7), served))  # diff_est * 3 + difficulty used for quality
        pts = []
        for j in sorted(range(len(mock)), key=lambda j: mock[j].usd_out):
            m = mock[j]
            q = m.quality
            pts.append(
                {
                    "name": f"always {m.id}",
                    "kind": "fixed",
                    "usd": round(sum(map(itemgetter(8 + j), served)), 6),
                    "expected_success": round(
                        sum(map([q[c % 3] for c in range(9)].__getitem__, codes)) / n, 4
                    ),
                }
            )
        # the router's figures were running totals (cost += ...), not sum(): a compensated sum
        # differs in the last bit, which moves a figure rounded to 6 places whenever the total
        # sits on a rounding boundary (token prices make that common)
        for mq, ch in zip(rr["thresholds"], rr["plans"], strict=True):
            if ch in rr["extra"]:
                k = rr["extra"][ch]
                costs, quals = map(itemgetter(k), served), map(itemgetter(k + 1), served)
            else:
                col = [8 + ch[est][0] for est in range(3)]  # cost column per estimate
                table = [mock[ch[c // 3][0]].quality[c % 3] for c in range(9)]
                costs = map(getitem, served, map(col.__getitem__, map(itemgetter(1), served)))
                quals = map(table.__getitem__, codes)
            pts.append(
                {
                    "name": f"router @ quality {mq:g}",
                    "kind": "router",
                    "usd": round(reduce(add, costs, 0.0), 6),
                    "expected_success": round(reduce(add, quals, 0.0) / n, 4),
                }
            )
        return {
            "ready": True,
            "n": n,
            "points": pts,
            "note": "Mock models only; calls that reached a model (cache hits excluded); token "
            "counts as recorded; success = the mock's quality table at the true difficulty "
            "where known, else the estimate.",
        }


@contextmanager
def window(
    store: Store, *, since=None, until=None, tenant: str = "", feature: str = "", buckets=24
) -> Iterator[Window]:
    """The calls of one window, read from one snapshot; ``buckets`` = points in the series."""
    with store.reader() as con:
        yield Window(
            con, since=since, until=until, tenant=tenant, feature=feature, buckets=buckets
        )


def obs_page(store: Store, *, since=None, until=None, tenant: str = "", feature: str = "") -> dict:
    """Everything /api/obs shows, from one snapshot."""
    with window(store, since=since, until=until, tenant=tenant, feature=feature) as w:
        summary = w.summary()
        if summary.get("calls"):
            summary["shadow_usd"] = w.shadow_usd()
            summary["total_usd"] = round(summary["usd"] + summary["shadow_usd"], 6)
        return {
            "summary": summary,
            "series": w.timeseries(),
            "by_tenant": w.with_shadow_spend(w.group_by("tenant"), "tenant"),
            "by_feature": w.with_shadow_spend(w.group_by("feature"), "feature"),
            "by_model": w.with_shadow_spend(w.group_by("model"), "model", add_missing=True),
            "drift": w.drift(),
        }


def routing_page(store: Store, models: list[ModelInfo], *, since=None, until=None) -> dict:
    """The router report and the cost/quality frontier /api/routing shows, from one snapshot."""
    with window(store, since=since, until=until) as w:
        rr = w._routing_rows(models)
        return {"report": w.routing_report(rr), "frontier": w.frontier(models, rr)}


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


