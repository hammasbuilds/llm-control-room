"""Releases: model and prompt versions with A/B, canary and shadow traffic, per-version SLOs,
and an auto-rollback that tells a bad canary from an upstream outage.

The structure follows model-serving-platform (champion / challenger / shadow, sticky hash
assignment, SLO window, "do not roll back when the champion is equally broken"). The part that
repo does not have is attributing a breach, because a canary system that rolls back during a
provider outage removes a healthy version and fixes nothing:

* a breach is *upstream* when the champion breaches the same limit too, or when the model the
  canary calls is failing for traffic that is not the canary;
* a quality breach can never be upstream;
* only breaches that are not upstream roll the canary back; an upstream breach holds the
  rollout and logs why.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field

from ._vendor.split import bucket
from .store import Store

DEFAULT_SLO = {
    "max_p95_ms": 4000.0,
    "max_error_rate": 0.05,
    "min_quality_rate": 0.50,  # absolute floor on the share of good answers
    "max_quality_drop": 0.15,  # and the most the challenger may trail the champion, if significant
    "min_samples": 40,
    "window": 120,
}


class ReleaseError(ValueError):
    pass


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    i = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered))) - 1))
    return round(ordered[i], 3)


@dataclass
class Obs:
    latency_ms: float
    failed: bool  # the call failed or had to fall back: the version's own model misbehaved
    upstream: bool  # ... and the cause was an upstream error
    quality_ok: bool | None
    cached: bool
    model: str


@dataclass
class Window:
    obs: deque = field(default_factory=lambda: deque(maxlen=120))

    def stats(self) -> dict:
        n = len(self.obs)
        lat = [o.latency_ms for o in self.obs if not o.cached and not o.failed]
        q = [o.quality_ok for o in self.obs if o.quality_ok is not None]
        failed = sum(o.failed for o in self.obs)
        up = sum(o.upstream for o in self.obs)
        return {
            "n": n,
            "error_rate": round(failed / n, 4) if n else 0.0,
            "upstream_share": round(up / failed, 4) if failed else 0.0,
            "p95_ms": percentile(lat, 95),
            "quality_rate": round(sum(q) / len(q), 4) if q else None,
            "models": sorted({o.model for o in self.obs if o.model}),
        }


class ModelHealth:
    """Recent provider attempts per (model, caller), so one caller's failures can be judged
    against what every other caller saw on the same model."""

    def __init__(self, size: int = 40) -> None:
        self.size = size
        self.attempts: dict[str, dict[str, deque]] = {}

    def record(self, model: str, ok: bool, tag: str) -> None:
        by_tag = self.attempts.setdefault(model, {})
        by_tag.setdefault(tag, deque(maxlen=self.size)).append(ok)

    def forget(self, prefix: str) -> None:
        """Drop everything a release's versions contributed (a recreated release starts clean)."""
        for by_tag in self.attempts.values():
            for tag in [t for t in by_tag if t.startswith(prefix)]:
                del by_tag[tag]

    def error_rate(self, model: str, exclude_tag: str = "", recent: int = 0) -> tuple[float, int]:
        """Error rate over other callers' attempts; ``recent`` keeps only each caller's last N,
        which is what shows an outage that has only just started."""
        rows = []
        for tag, d in self.attempts.get(model, {}).items():
            if tag != exclude_tag:
                rows += list(d)[-recent:] if recent else list(d)
        return (round(rows.count(False) / len(rows), 4) if rows else 0.0), len(rows)


class Releases:
    def __init__(self, store: Store, clock=time.time) -> None:
        self.store = store
        self.clock = clock
        self.windows: dict[tuple[str, int], Window] = {}
        self.health = ModelHealth()
        self.last_verdict: dict[str, str] = {}
        self.last_check: dict[str, dict] = {}

    # ------------------------------------------------------------------ CRUD

    def event(self, release: str, kind: str, **detail) -> None:
        self.store.run(
            "INSERT INTO release_events(at, release, kind, detail) VALUES(?,?,?,?)",
            (self.clock(), release, kind, json.dumps(detail)),
        )

    def create(
        self,
        name: str,
        *,
        model: str = "auto",
        system_prompt: str = "",
        note: str = "",
        slo: dict | None = None,
        auto_rollback: bool = True,
    ) -> dict:
        if not name or not name.replace("-", "").replace("_", "").isalnum() or len(name) > 40:
            raise ReleaseError("release name must be letters, digits, - or _")
        if self.store.one("SELECT 1 FROM releases WHERE name=?", (name,)):
            raise ReleaseError(f"release {name!r} already exists")
        merged = {**DEFAULT_SLO, **(slo or {})}
        self.store.run(
            "INSERT INTO releases(name, created, auto_rollback, slo) VALUES(?,?,?,?)",
            (name, self.clock(), int(auto_rollback), json.dumps(merged)),
        )
        self.store.run(
            "INSERT INTO versions(release, version, model, system_prompt, note, stage,"
            " traffic, created) VALUES(?,?,?,?,?,?,?,?)",
            (name, 1, model, system_prompt, note or "first version", "champion", 1.0, self.clock()),
        )
        self.event(name, "created", version=1, model=model)
        return self.get(name)

    def delete(self, name: str) -> None:
        with self.store.transaction():
            self.store.run("DELETE FROM versions WHERE release=?", (name,))
            self.store.run("DELETE FROM releases WHERE name=?", (name,))
            self.store.run("DELETE FROM release_events WHERE release=?", (name,))
            self.store.run("DELETE FROM calls WHERE release=?", (name,))
        for k in [k for k in self.windows if k[0] == name]:
            del self.windows[k]
        self.health.forget(f"{name}:")
        self.last_verdict.pop(name, None)
        self.last_check.pop(name, None)

    def add_version(
        self, name: str, *, model: str = "auto", system_prompt: str = "", note: str = ""
    ) -> dict:
        self._rel(name)
        n = (
            self.store.one("SELECT MAX(version) AS m FROM versions WHERE release=?", (name,))["m"]
            + 1
        )
        self.store.run(
            "INSERT INTO versions(release, version, model, system_prompt, note, stage,"
            " traffic, created) VALUES(?,?,?,?,?,?,?,?)",
            (name, n, model, system_prompt, note, "archived", 0.0, self.clock()),
        )
        self.event(name, "version_added", version=n, model=model, note=note)
        return self.version(name, n)

    def _rel(self, name: str) -> dict:
        r = self.store.one("SELECT * FROM releases WHERE name=?", (name,))
        if not r:
            raise ReleaseError(f"no release {name!r}")
        r["slo"] = {**DEFAULT_SLO, **json.loads(r["slo"])}
        r["auto_rollback"] = bool(r["auto_rollback"])
        return r

    def version(self, name: str, v: int) -> dict:
        r = self.store.one("SELECT * FROM versions WHERE release=? AND version=?", (name, v))
        if not r:
            raise ReleaseError(f"no version {v} in release {name!r}")
        return r

    def versions(self, name: str) -> list[dict]:
        return self.store.all("SELECT * FROM versions WHERE release=? ORDER BY version", (name,))

    def get(self, name: str) -> dict:
        r = self._rel(name)
        r["versions"] = self.versions(name)
        r["champion"] = next(
            (v["version"] for v in r["versions"] if v["stage"] == "champion"), None
        )
        ch = next((v for v in r["versions"] if v["stage"] == "challenger"), None)
        r["challenger"] = ch["version"] if ch else None
        r["shadows"] = [v["version"] for v in r["versions"] if v["stage"] == "shadow"]
        r["last_verdict"] = self.last_verdict.get(name, "")
        return r

    def list(self) -> list[dict]:
        return [
            self.get(r["name"]) for r in self.store.all("SELECT name FROM releases ORDER BY name")
        ]

    def events(self, name: str, limit: int = 100) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM release_events WHERE release=? ORDER BY id DESC LIMIT ?", (name, limit)
        )
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    def set_slo(self, name: str, slo: dict, auto_rollback: bool | None = None) -> dict:
        r = self._rel(name)
        merged = {**r["slo"], **{k: float(v) for k, v in slo.items() if k in DEFAULT_SLO}}
        self.store.run("UPDATE releases SET slo=? WHERE name=?", (json.dumps(merged), name))
        if auto_rollback is not None:
            self.store.run(
                "UPDATE releases SET auto_rollback=? WHERE name=?", (int(auto_rollback), name)
            )
        return self.get(name)

    # ------------------------------------------------------------------ rollout actions

    def _stage_of(self, name: str, v: int) -> str:
        return self.version(name, v)["stage"]

    def start_canary(self, name: str, v: int, traffic: float = 0.1, mode: str = "canary") -> dict:
        if mode not in ("canary", "ab"):
            raise ReleaseError("mode must be canary or ab")
        if not 0.0 < traffic < 1.0:
            raise ReleaseError("traffic must be between 0 and 1, exclusive")
        r = self.get(name)
        if r["champion"] is None:
            raise ReleaseError("no champion to compare against")
        if v == r["champion"]:
            raise ReleaseError("that version is already the champion")
        if r["challenger"] not in (None, v):
            raise ReleaseError(
                f"v{r['challenger']} is already the challenger; one at a time or "
                "the comparison means nothing"
            )
        self.store.run(
            "UPDATE versions SET stage='challenger', traffic=? WHERE release=? AND version=?",
            (traffic, name, v),
        )
        self.store.run(
            "UPDATE releases SET challenger_mode=?, auto_rollback=? WHERE name=?",
            (mode, int(mode == "canary"), name),
        )
        self.windows.pop((name, v), None)
        self.health.forget(f"{name}:{v}")
        self.last_verdict.pop(name, None)
        self.last_check.pop(name, None)
        self.event(
            name, "canary_started" if mode == "canary" else "ab_started", version=v, traffic=traffic
        )
        return self.get(name)

    def set_traffic(self, name: str, traffic: float) -> dict:
        r = self.get(name)
        if r["challenger"] is None:
            raise ReleaseError("no challenger")
        if not 0.0 < traffic <= 1.0:
            raise ReleaseError("traffic must be in (0, 1]")
        self.store.run(
            "UPDATE versions SET traffic=? WHERE release=? AND version=?",
            (traffic, name, r["challenger"]),
        )
        self.event(name, "traffic_changed", version=r["challenger"], traffic=traffic)
        return self.get(name)

    def add_shadow(self, name: str, v: int) -> dict:
        stage = self._stage_of(name, v)
        if stage in ("champion", "challenger"):
            raise ReleaseError(f"v{v} is the {stage}; it cannot also shadow")
        self.store.run(
            "UPDATE versions SET stage='shadow', traffic=0 WHERE release=? AND version=?", (name, v)
        )
        self.windows.pop((name, v), None)
        self.event(name, "shadow_started", version=v)
        return self.get(name)

    def remove_shadow(self, name: str, v: int) -> dict:
        if self._stage_of(name, v) != "shadow":
            raise ReleaseError(f"v{v} is not a shadow")
        self.store.run(
            "UPDATE versions SET stage='archived' WHERE release=? AND version=?", (name, v)
        )
        self.event(name, "shadow_stopped", version=v)
        return self.get(name)

    def promote(self, name: str, v: int, reason: str = "manual") -> dict:
        self._stage_of(name, v)
        with self.store.transaction():
            self.store.run(
                "UPDATE versions SET stage='archived', traffic=0 WHERE release=? "
                "AND stage='champion'",
                (name,),
            )
            self.store.run(
                "UPDATE versions SET stage='champion', traffic=1 WHERE release=? AND version=?",
                (name, v),
            )
        self.last_verdict.pop(name, None)
        self.last_check.pop(name, None)
        self.event(name, "promoted", version=v, reason=reason)
        return self.get(name)

    def rollback(self, name: str, reason: str = "manual", auto: bool = False) -> dict:
        r = self.get(name)
        if r["challenger"] is None:
            raise ReleaseError("no challenger to roll back")
        self.store.run(
            "UPDATE versions SET stage='archived', traffic=0 WHERE release=? AND version=?",
            (name, r["challenger"]),
        )
        self.event(
            name, "auto_rollback" if auto else "rolled_back", version=r["challenger"], reason=reason
        )
        return self.get(name)

    # ------------------------------------------------------------------ routing

    def resolve(self, name: str, key: str) -> tuple[dict, list[dict]]:
        """(version to serve, shadow versions). Sticky: the same key always lands on one arm."""
        r = self.get(name)
        champion = next(v for v in r["versions"] if v["stage"] == "champion")
        served = champion
        challenger = next((v for v in r["versions"] if v["stage"] == "challenger"), None)
        if challenger and bucket(key, salt=name) < challenger["traffic"]:
            served = challenger
        shadows = [v for v in r["versions"] if v["stage"] == "shadow"]
        return served, shadows

    def exists(self, name: str) -> bool:
        return self.store.one("SELECT 1 FROM releases WHERE name=?", (name,)) is not None

    # ------------------------------------------------------------------ observing

    def observe(self, name: str, v: int, o: Obs) -> None:
        w = self.windows.get((name, v))
        if w is None:
            size = int(self._rel(name)["slo"]["window"])
            w = self.windows[(name, v)] = Window(deque(maxlen=size))
        w.obs.append(o)

    def window_stats(self, name: str, v: int) -> dict:
        return self.windows.get((name, v), Window()).stats()

    def check(self, name: str) -> dict:
        out = self._check(name)
        if out["verdict"] != "no_challenger":
            self.last_check[name] = out
        return out

    def _check(self, name: str) -> dict:
        """Judge the challenger against the SLO and, if warranted, roll it back."""
        r = self.get(name)
        ch_v, cp_v = r["challenger"], r["champion"]
        if ch_v is None:
            return {"verdict": "no_challenger"}
        slo = r["slo"]
        ch = self.window_stats(name, ch_v)
        cp = self.window_stats(name, cp_v)
        out = {"verdict": "ok", "challenger": ch, "champion": cp, "breaches": []}
        if ch["n"] < slo["min_samples"]:
            out["verdict"] = "insufficient_samples"
            return out

        champ_ready = cp["n"] >= slo["min_samples"]

        def breach(s: dict, versus: dict | None = None) -> list[str]:
            b = []
            if s["error_rate"] > slo["max_error_rate"]:
                b.append("error_rate")
            if s["p95_ms"] > slo["max_p95_ms"]:
                b.append("latency")
            q = s["quality_rate"]
            if q is not None:
                if q < slo["min_quality_rate"]:
                    b.append("quality")
                elif (
                    versus
                    and versus["quality_rate"] is not None
                    and versus["n"] >= slo["min_samples"]
                ):
                    # trailing the champion counts only when the gap is large and p < 0.001 (strict: the check runs on every request, so a loose test would eventually fire on noise)
                    gap = versus["quality_rate"] - q
                    _, p = two_proportion_z(versus["quality_rate"], versus["n"], q, s["n"])
                    if gap > slo["max_quality_drop"] and p < 0.001:
                        b.append("quality")
            return b

        mine = breach(ch, cp)
        if not mine:
            self.last_verdict[name] = "ok"
            return out
        theirs = breach(cp) if champ_ready else []
        # health of the model(s) the challenger calls, judged on traffic that is not the canary
        tag = f"{name}:{ch_v}"
        sick, unknown, healthy = [], [], []
        for m in ch["models"]:
            rate, n = self.health.error_rate(m, exclude_tag=tag)
            rrate, rn = self.health.error_rate(m, exclude_tag=tag, recent=12)
            row = {"model": m, "error_rate": rate, "attempts": n}
            if n < 10:
                unknown.append(row)
            elif rate > slo["max_error_rate"]:
                sick.append(row)
            elif rn >= 8 and rrate > 0.25:  # an outage that began a moment ago
                sick.append({"model": m, "error_rate": rrate, "attempts": rn})
            else:
                healthy.append(row)
        details = []
        upstream_all = True
        for b in mine:
            if b == "quality":
                upstream, why = (
                    False,
                    "answer quality fell below the SLO; an outage does not change answers",
                )
            elif b == "error_rate" and ch["upstream_share"] < 0.5:
                upstream, why = False, "most failures were not upstream errors"
            elif b in theirs:
                upstream, why = True, f"the champion breaches {b} too, so the cause is shared"
            elif sick and b in ("error_rate", "latency"):
                upstream = True
                why = (
                    f"{sick[0]['model']} is failing for other traffic too "
                    f"({sick[0]['error_rate']:.0%} of {sick[0]['attempts']} attempts)"
                )
            elif b == "error_rate" and unknown and not healthy:
                upstream = True
                why = (
                    f"{unknown[0]['model']} has no other traffic to compare with, so a provider "
                    "outage cannot be ruled out; holding rather than guessing"
                )
            elif b == "error_rate" and healthy:
                h = healthy[0]
                _, p_val = two_proportion_z(
                    h["error_rate"], h["attempts"], ch["error_rate"], ch["n"]
                )
                if ch["error_rate"] > h["error_rate"] and p_val < 0.01:
                    upstream, why = (
                        False,
                        (
                            f"{h['model']} is healthy for other traffic ({h['error_rate']:.0%} errors "
                            f"over {h['attempts']} attempts) but fails {ch['error_rate']:.0%} here "
                            f"(p={p_val:.4f})"
                        ),
                    )
                else:
                    upstream, why = (
                        True,
                        (
                            f"{h['model']} errors here ({ch['error_rate']:.0%}) are not distinguishable "
                            f"from its errors elsewhere ({h['error_rate']:.0%}, p={p_val:.2f}); "
                            "holding rather than guessing"
                        ),
                    )
            else:
                upstream, why = False, "the champion is healthy on the same traffic mix"
            upstream_all &= upstream
            details.append({"breach": b, "upstream": upstream, "why": why})
        out["breaches"] = details
        if upstream_all:
            out["verdict"] = "upstream_outage"
            if self.last_verdict.get(name) != "upstream_outage":
                self.event(
                    name,
                    "rollback_held",
                    version=ch_v,
                    breaches=details,
                    note="upstream outage: not rolling back a healthy version",
                )
            self.last_verdict[name] = "upstream_outage"
            return out
        out["verdict"] = "bad_canary"
        self.last_verdict[name] = "bad_canary"
        if r["auto_rollback"]:
            reason = "; ".join(f"{d['breach']}: {d['why']}" for d in details)
            self.rollback(name, reason=f"SLO breach on v{ch_v} ({reason})", auto=True)
            out["verdict"] = "rolled_back"
        return out

    # ------------------------------------------------------------------ analysis

    def analysis(self, name: str) -> dict:
        r = self.get(name)
        arms = []
        for v in r["versions"]:
            rows = self.store.all(
                "SELECT usd, latency_ms, cached, error, fallback_used, quality_ok, shadow, shadow_sim "
                "FROM calls WHERE release=? AND version=?",
                (name, v["version"]),
            )
            live = [x for x in rows if not x["shadow"]]
            sh = [x for x in rows if x["shadow"]]
            if not rows:
                continue
            base = live or sh
            n = len(base)
            known = [
                x["quality_ok"] for x in base if x["quality_ok"] is not None and not x["error"]
            ]
            ok = sum(known)
            lat = [x["latency_ms"] for x in base if not x["cached"] and not x["error"]]
            arms.append(
                {
                    "version": v["version"],
                    "stage": v["stage"],
                    "model": v["model"],
                    "shadow": bool(sh and not live),
                    "n": n,
                    "error_rate": round(sum(1 for x in base if x["error"]) / n, 4),
                    "success_rate": round(ok / len(known), 4) if known else None,
                    "success_n": len(known),
                    "usd_per_call": round(sum(x["usd"] for x in base) / n, 8),
                    "p50_ms": percentile(lat, 50),
                    "p95_ms": percentile(lat, 95),
                    "agreement": (
                        round(sum(x["shadow_sim"] or 0 for x in sh) / len(sh), 4) if sh else None
                    ),
                }
            )
        out = {"release": name, "arms": arms, "comparison": None}
        champ = next((a for a in arms if a["version"] == r["champion"] and not a["shadow"]), None)
        chall = next((a for a in arms if a["version"] == r["challenger"]), None)
        if champ and chall and champ["success_n"] and chall["success_n"]:
            z, p = two_proportion_z(
                champ["success_rate"], champ["success_n"], chall["success_rate"], chall["success_n"]
            )
            min_n = min(champ["success_n"], chall["success_n"])
            if min_n < 30:
                verdict = "insufficient data"
            elif p < 0.05:
                verdict = (
                    "challenger better"
                    if chall["success_rate"] > champ["success_rate"]
                    else "challenger worse"
                )
            else:
                verdict = "no significant difference"
            out["comparison"] = {
                "z": round(z, 3),
                "p_value": round(p, 4),
                "verdict": verdict,
                "success_delta": round(chall["success_rate"] - champ["success_rate"], 4),
                "cost_delta_per_call": round(chall["usd_per_call"] - champ["usd_per_call"], 8),
                "p95_delta_ms": round(chall["p95_ms"] - champ["p95_ms"], 1),
            }
        return out


def two_proportion_z(p1: float, n1: int, p2: float, n2: int) -> tuple[float, float]:
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(max(pooled * (1 - pooled), 1e-12) * (1 / n1 + 1 / n2))
    z = (p2 - p1) / se
    p = math.erfc(abs(z) / math.sqrt(2))
    return z, p
