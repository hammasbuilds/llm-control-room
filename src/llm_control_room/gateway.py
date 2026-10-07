"""The gateway: one OpenAI-compatible entry point, every policy applied in a fixed order.

    guard(in) -> rate limit -> route -> budget -> cache -> call with fallback -> guard(out) -> record

Guardrails run first so a blocked request costs nothing and a secret never leaves. The budget
check comes after routing because it needs the price of the model the router picked, and before
the cache so an over-budget tenant is refused rather than quietly served from memory.
No prompt text is stored: only token counts, a shape signature and the findings.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from . import router as routing
from ._vendor.guardrails import check_input, check_output
from ._vendor.signature import signature
from .providers import ChatRequest, ProviderError, ProviderSet, content_words, count_tokens
from .releases import Obs, Releases
from .store import Store
from .tenants import Tenants

CACHE_HIT_MS = 2.0


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


@dataclass
class GatewayRequest:
    tenant: str
    messages: list[dict]
    model: str = "auto"
    max_tokens: int = 512
    temperature: float = 0.0
    feature: str = "default"
    context: str = ""
    use_cache: bool = True
    session_key: str = ""
    true_difficulty: int | None = None
    source: str = "api"
    dry_run: bool = False


def normalise_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        out.append({"role": str(m.get("role", "user")), "content": str(content)})
    if not any(m["role"] == "user" and m["content"].strip() for m in out):
        raise GatewayError(400, "bad_request", "messages must contain a non-empty user message")
    return out


def grounding_score(answer: str, context: str) -> float:
    """Share of the answer's sentences whose content words mostly appear in the context."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if content_words(s)]
    if not sentences:
        return 1.0
    ctx = content_words(context)
    supported = 0
    for s in sentences:
        words = content_words(s)
        if len(words & ctx) / len(words) >= 0.6:
            supported += 1
    return round(supported / len(sentences), 4)


def text_similarity(a: str, b: str) -> float:
    wa, wb = content_words(a), content_words(b)
    if not wa and not wb:
        return 1.0
    return round(len(wa & wb) / len(wa | wb), 4)


class ResponseCache:
    def __init__(self, clock=time.time, max_entries: int = 2000) -> None:
        self.clock, self.max_entries = clock, max_entries
        self.data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key: str, ttl: float) -> dict | None:
        item = self.data.get(key)
        if item and self.clock() - item[0] <= ttl:
            self.data.move_to_end(key)
            self.hits += 1
            return item[1]
        if item:
            del self.data[key]
        self.misses += 1
        return None

    def put(self, key: str, value: dict) -> None:
        self.data[key] = (self.clock(), value)
        self.data.move_to_end(key)
        while len(self.data) > self.max_entries:
            self.data.popitem(last=False)

    def clear(self) -> None:
        self.data.clear()
        self.hits = self.misses = 0


@dataclass
class Gateway:
    store: Store
    providers: ProviderSet
    tenants: Tenants
    releases: Releases
    clock: object = time.time
    cache: ResponseCache = field(default=None)  # type: ignore[assignment]
    on_call: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = ResponseCache(lambda: self.clock())

    # ------------------------------------------------------------------ settings

    def setting(self, key: str, default):
        return self.store.kv_get(f"setting:{key}", default)

    # ------------------------------------------------------------------ entry point

    def handle(self, req: GatewayRequest) -> dict:
        tenant = self.tenants.get(req.tenant)
        if tenant is None:
            raise GatewayError(401, "unknown_tenant", f"no tenant {req.tenant!r}")
        started_at = self.clock()
        messages = normalise_messages(req.messages)
        trace = uuid.uuid4().hex[:16]
        base = {
            "trace": trace,
            "at": started_at,
            "tenant": req.tenant,
            "feature": req.feature,
            "requested": req.model,
            "diff_true": req.true_difficulty,
            "source": req.source,
        }

        # 1. guardrails: block injection, redact secrets (and PII when the tenant asks)
        redactions: list[str] = []
        guarded = []
        for m in messages:
            res = check_input(
                m["content"], redact_pii=tenant["redact_pii"], block_injection=m["role"] == "user"
            )
            if not res.allowed:
                self._record(
                    base,
                    error="blocked: " + ", ".join(res.findings),
                    error_kind="blocked",
                    prompt_len=len(m["content"]),
                )
                raise GatewayError(
                    400, "blocked", "request blocked by input guardrails", findings=res.findings
                )
            redactions += res.findings
            guarded.append({"role": m["role"], "content": res.text})
        context = req.context
        if context:
            cres = check_input(context, redact_pii=tenant["redact_pii"])
            if not cres.allowed:
                self._record(
                    base, error="blocked: " + ", ".join(cres.findings), error_kind="blocked"
                )
                raise GatewayError(
                    400,
                    "blocked",
                    "retrieved context blocked by input guardrails",
                    findings=cres.findings,
                )
            context = cres.text
            redactions += cres.findings
        redactions = sorted(set(redactions))
        user_text = next(m["content"] for m in reversed(guarded) if m["role"] == "user")
        route_text = "\n".join(m["content"] for m in guarded if m["role"] == "user")

        # 2. rate limit
        window = 60.0
        used = self.store.one(
            "SELECT COUNT(*) AS n FROM calls WHERE tenant=? AND at>? AND at<=? AND "
            "shadow=0 AND error_kind NOT IN ('rate_limited','budget','blocked')",
            (req.tenant, started_at - window, started_at),
        )["n"]
        if used >= tenant["rpm"]:
            self._record(
                base, error="rate limited", error_kind="rate_limited", redactions=redactions
            )
            raise GatewayError(429, "rate_limited", f"{tenant['rpm']} requests per minute exceeded")

        # 3. release resolution (a model name that is a release picks a version)
        release = ""
        version = 0
        shadows: list[dict] = []
        model_choice = req.model or "auto"
        sys_prefix = ""
        if model_choice != "auto" and self.releases.exists(model_choice):
            release = model_choice
            key = req.session_key or hashlib.sha256(user_text.encode()).hexdigest()[:16]
            served, shadows = self.releases.resolve(release, f"{req.tenant}:{key}")
            version = served["version"]
            model_choice = served["model"]
            sys_prefix = served["system_prompt"]
        send = ([{"role": "system", "content": sys_prefix}] if sys_prefix else []) + guarded

        # 4. route
        models = self.providers.models()
        pinned = None if model_choice == "auto" else model_choice
        try:
            decision = routing.decide(
                route_text,
                models,
                context=context,
                min_quality=tenant["min_quality"],
                allowed=tenant["allowed_models"] or None,
                pinned=pinned,
                extra_fallbacks=tenant["fallbacks"],
                baseline=self.setting("baseline_model", ""),
            )
        except ValueError as exc:
            raise GatewayError(400, "bad_request", str(exc)) from exc
        if pinned and tenant["allowed_models"] and pinned not in tenant["allowed_models"]:
            raise GatewayError(403, "model_not_allowed", f"{pinned} is not allowed for this tenant")
        base.update(
            release=release,
            version=version,
            diff_est=decision.level,
            route_reason=decision.reason,
            prompt_len=len(user_text),
            signature=signature(user_text),
        )

        if req.dry_run:
            return {
                "route": decision.to_dict(),
                "redactions": redactions,
                "release": release,
                "version": version,
            }

        # 5. budget
        spent = self.store.one(
            "SELECT COALESCE(SUM(usd),0) AS s FROM calls WHERE tenant=? AND at>? AND at<=? "
            "AND shadow=0",
            (req.tenant, started_at - tenant["budget_window_s"], started_at),
        )["s"]
        if spent + decision.expected_usd > tenant["budget_usd"]:
            self._record(base, error="budget exhausted", error_kind="budget", redactions=redactions)
            raise GatewayError(
                429,
                "budget_exceeded",
                f"tenant budget ${tenant['budget_usd']:.4f} reached (spent ${spent:.4f})",
                spent=round(spent, 6),
                limit=tenant["budget_usd"],
            )

        # 6. cache
        cacheable = req.use_cache and tenant["cache_enabled"] and req.temperature == 0
        ckey = hashlib.sha256(
            json.dumps(
                [req.tenant, decision.primary, send, context, req.max_tokens], sort_keys=True
            ).encode()
        ).hexdigest()
        if cacheable:
            hit = self.cache.get(ckey, self.setting("cache_ttl_s", 3600))
            if hit:
                info = self.providers.info(hit["model"])
                base_info = self.providers.info(decision.baseline_model)
                rec = self._record(
                    base,
                    model=hit["model"],
                    provider=info.provider if info else "",
                    cached=1,
                    prompt_tokens=hit["prompt_tokens"],
                    completion_tokens=hit["completion_tokens"],
                    baseline_usd=base_info.cost(hit["prompt_tokens"], hit["completion_tokens"])
                    if base_info
                    else 0.0,
                    latency_ms=CACHE_HIT_MS,
                    redactions=redactions,
                    grounding=hit["grounding"],
                    quality_ok=hit["quality_ok"],
                )
                self._release_observe(release, version, rec, "")
                return self._result(rec, hit["text"], decision, redactions, [], hit["model"])

        # 7. call with fallback
        chat = ChatRequest(
            send, req.max_tokens, req.temperature, context, req.true_difficulty, decision.level
        )
        if context:
            chat.messages = [
                *send[:-1],
                {
                    "role": "user",
                    "content": f"Context:\n{context}\n\nQuestion: {send[-1]['content']}",
                },
            ]
            chat.context = context
        tag = f"{release}:{version}" if release else ""
        attempts: list[dict] = []
        comp = None
        used_model = ""
        for model in [decision.primary, *decision.chain]:
            prov = self.providers.provider_for(model)
            if prov is None:
                attempts.append({"model": model, "error": "no provider"})
                continue
            try:
                comp = prov.complete(chat, model)
            except ProviderError as exc:
                attempts.append({"model": model, "error": str(exc)})
                self.releases.health.record(model, False, tag)
                continue
            self.releases.health.record(model, True, tag)
            used_model = model
            break

        if comp is None:
            rec = self._record(
                base,
                error="all providers failed: " + "; ".join(a["model"] for a in attempts),
                error_kind="upstream",
                attempts=attempts,
                fallback_used=1 if len(attempts) > 1 else 0,
                redactions=redactions,
                latency_ms=0.0,
                model=decision.primary,
            )
            self._release_observe(release, version, rec, tag)
            raise GatewayError(
                502,
                "all_providers_failed",
                "every provider in the chain failed",
                attempts=attempts,
                trace=trace,
            )

        # 8. output guard, cost, grounding
        out = check_output(comp.text)
        text = out.text
        redactions = sorted(set(redactions + out.findings))
        info = self.providers.info(used_model)
        base_info = self.providers.info(decision.baseline_model)
        usd = info.cost(comp.prompt_tokens, comp.completion_tokens) if info else 0.0
        baseline_usd = (
            base_info.cost(comp.prompt_tokens, comp.completion_tokens) if base_info else usd
        )
        grounding = grounding_score(text, context) if context else None
        # failed attempts before the success are real latency the caller waited for
        latency = comp.latency_ms + 25.0 * len(attempts)
        rec = self._record(
            base,
            model=used_model,
            provider=info.provider if info else "",
            prompt_tokens=comp.prompt_tokens,
            completion_tokens=comp.completion_tokens,
            usd=usd,
            baseline_usd=baseline_usd,
            latency_ms=latency,
            fallback_used=1 if attempts else 0,
            attempts=attempts,
            redactions=redactions,
            grounding=grounding,
            quality_ok=None if comp.quality_ok is None else int(comp.quality_ok),
        )
        if cacheable:
            self.cache.put(
                ckey,
                {
                    "text": text,
                    "model": used_model,
                    "prompt_tokens": comp.prompt_tokens,
                    "completion_tokens": comp.completion_tokens,
                    "grounding": grounding,
                    "quality_ok": rec["quality_ok"],
                },
            )
        self._release_observe(release, version, rec, tag)

        # 9. shadows: same request, answer recorded and never returned, failures contained
        for sv in shadows:
            try:
                self._run_shadow(release, sv, req, guarded, context, user_text, tenant, text, trace)
            except Exception:  # a shadow must never be able to break serving
                pass
        if release:
            self.releases.check(release)
        for cb in self.on_call:
            cb()
        return self._result(rec, text, decision, redactions, attempts, used_model)

    # ------------------------------------------------------------------ helpers

    def _run_shadow(
        self, release, sv, req, guarded, context, user_text, tenant, served_text, trace
    ):
        send = (
            [{"role": "system", "content": sv["system_prompt"]}] if sv["system_prompt"] else []
        ) + guarded
        pinned = None if sv["model"] == "auto" else sv["model"]
        d = routing.decide(
            user_text,
            self.providers.models(),
            context=context,
            min_quality=tenant["min_quality"],
            allowed=tenant["allowed_models"] or None,
            pinned=pinned,
        )
        chat = ChatRequest(send, req.max_tokens, 0.0, context, req.true_difficulty, d.level)
        if context:
            chat.messages = [
                *send[:-1],
                {
                    "role": "user",
                    "content": f"Context:\n{context}\n\nQuestion: {send[-1]['content']}",
                },
            ]
        tag = f"{release}:{sv['version']}"
        base = {
            "trace": trace,
            "at": self.clock(),
            "tenant": req.tenant,
            "feature": req.feature,
            "requested": release,
            "diff_true": req.true_difficulty,
            "release": release,
            "version": sv["version"],
            "diff_est": d.level,
            "route_reason": d.reason,
            "prompt_len": len(user_text),
            "signature": signature(user_text),
            "shadow": 1,
            "source": req.source,
        }
        prov = self.providers.provider_for(d.primary)
        try:
            comp = prov.complete(chat, d.primary)
        except ProviderError as exc:
            self.releases.health.record(d.primary, False, tag)
            rec = self._record(base, model=d.primary, error=str(exc), error_kind="upstream")
            self.releases.observe(
                release, sv["version"], Obs(0, True, True, None, False, d.primary)
            )
            return rec
        self.releases.health.record(d.primary, True, tag)
        info = self.providers.info(d.primary)
        usd = info.cost(comp.prompt_tokens, comp.completion_tokens)
        rec = self._record(
            base,
            model=d.primary,
            provider=info.provider,
            prompt_tokens=comp.prompt_tokens,
            completion_tokens=comp.completion_tokens,
            usd=usd,
            latency_ms=comp.latency_ms,
            grounding=grounding_score(comp.text, context) if context else None,
            quality_ok=None if comp.quality_ok is None else int(comp.quality_ok),
            shadow_sim=text_similarity(comp.text, served_text),
        )
        self.releases.observe(
            release,
            sv["version"],
            Obs(comp.latency_ms, False, False, comp.quality_ok, False, d.primary),
        )
        return rec

    def _release_observe(self, release: str, version: int, rec: dict, tag: str) -> None:
        if not release:
            return
        failed = bool(rec["error"]) or bool(rec["fallback_used"])
        # the model the version asked for, not the fallback that finally answered
        asked = rec["attempts"][0]["model"] if rec["attempts"] else rec["model"]
        self.releases.observe(
            release,
            version,
            Obs(
                rec["latency_ms"],
                failed,
                failed and (rec["error_kind"] in ("", "upstream")),
                None if rec["quality_ok"] is None else bool(rec["quality_ok"]),
                bool(rec["cached"]),
                asked,
            ),
        )

    COLS = (
        "trace",
        "at",
        "tenant",
        "feature",
        "release",
        "version",
        "requested",
        "model",
        "provider",
        "route_reason",
        "diff_est",
        "diff_true",
        "prompt_tokens",
        "completion_tokens",
        "usd",
        "baseline_usd",
        "latency_ms",
        "cached",
        "fallback_used",
        "attempts",
        "error",
        "error_kind",
        "redactions",
        "grounding",
        "quality_ok",
        "prompt_len",
        "signature",
        "shadow",
        "shadow_sim",
        "source",
    )

    def _record(self, base: dict, **kw) -> dict:
        rec = {c: None for c in self.COLS}
        rec.update(
            {
                "release": "",
                "version": 0,
                "requested": "",
                "model": "",
                "provider": "",
                "route_reason": "",
                "diff_est": 1,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "usd": 0.0,
                "baseline_usd": 0.0,
                "latency_ms": 0.0,
                "cached": 0,
                "fallback_used": 0,
                "attempts": [],
                "error": "",
                "error_kind": "",
                "redactions": [],
                "prompt_len": 0,
                "signature": "",
                "shadow": 0,
            }
        )
        rec.update(base)
        rec.update(kw)
        row = dict(rec)
        row["attempts"] = json.dumps(rec["attempts"])
        row["redactions"] = json.dumps(rec["redactions"])
        row["cached"] = int(rec["cached"])
        self.store.run(
            f"INSERT INTO calls({','.join(self.COLS)}) VALUES({','.join('?' * len(self.COLS))})",
            [row[c] for c in self.COLS],
        )
        return rec

    def _result(self, rec, text, decision, redactions, attempts, model) -> dict:
        info = self.providers.info(model)
        return {
            "id": "chatcmpl-" + rec["trace"],
            "trace": rec["trace"],
            "text": text,
            "model": model,
            "provider": info.provider if info else "",
            "cached": bool(rec["cached"]),
            "usage": {
                "prompt_tokens": rec["prompt_tokens"],
                "completion_tokens": rec["completion_tokens"],
                "total_tokens": rec["prompt_tokens"] + rec["completion_tokens"],
                "usd": round(rec["usd"], 8),
                "baseline_usd": round(rec["baseline_usd"], 8),
                "saved_usd": round(rec["baseline_usd"] - rec["usd"], 8),
            },
            "latency_ms": rec["latency_ms"],
            "route": decision.to_dict(),
            "attempts": attempts,
            "fallback_used": bool(rec["fallback_used"]),
            "redactions": redactions,
            "release": rec["release"],
            "version": rec["version"],
            "grounding": rec["grounding"],
            "quality_ok": None if rec["quality_ok"] is None else bool(rec["quality_ok"]),
        }


__all__ = ["Gateway", "GatewayError", "GatewayRequest", "count_tokens"]
