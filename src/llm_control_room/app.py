"""HTTP surface: the OpenAI-compatible gateway on /v1 and the admin API on /api."""

from __future__ import annotations

import csv
import hmac
import io
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import obs, sandbox
from .core import DEMO_TENANTS, Core, demo_key
from .gateway import MAX_CHARS, GatewayError, GatewayRequest
from .providers import ProviderSet
from .releases import ReleaseError
from .simulator import SCENARIOS as SIM_SCENARIOS
from .simulator import Simulator
from .store import Store
from .tenants import TenantError

STATIC = Path(__file__).parent / "static"
VERSION = "0.2.0"
MAX_BODY = 1_000_000  # bytes accepted on any request
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class Throttle:
    """A sliding-window counter per client, for failed logins (admin token or API key)."""

    def __init__(self, limit: int = 20, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def blocked(self, who: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.get(who)
            while q and now - q[0] > self.window:
                q.popleft()
            return bool(q) and len(q) >= self.limit

    def fail(self, who: str) -> None:
        with self.lock:
            if len(self.hits) > 5000:
                self.hits.clear()
            self.hits.setdefault(who, deque()).append(time.monotonic())


class SecurityMiddleware:
    """Checks that make a local control plane safe to leave running in a browser.

    * Host header must be one this server answers to, which defeats DNS rebinding (a web page
      re-pointing its own name at 127.0.0.1 to read the admin API).
    * A state-changing request that carries an Origin must come from this server's own origin, and
      a browser that says it is cross-site (Sec-Fetch-Site) is refused on /api.
    * A request with a body must be application/json, so a cross-origin page cannot send it as a
      "simple" form post that skips the CORS preflight.
    * Bodies over MAX_BODY bytes are refused before they are read into memory.
    """

    def __init__(self, app, allowed_hosts: frozenset[str] | None) -> None:
        self.app, self.allowed_hosts = app, allowed_hosts

    async def _refuse(self, send, status: int, code: str, message: str) -> None:
        body = json.dumps({"error": {"code": code, "message": message}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = h.get("host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if self.allowed_hosts is not None and name.lower() not in self.allowed_hosts:
            return await self._refuse(send, 403, "bad_host", f"host {name!r} is not allowed")
        method, path = scope["method"], scope["path"]
        if method not in ("GET", "HEAD", "OPTIONS"):
            origin = h.get("origin")
            if origin and origin != "null" and origin.split("://", 1)[-1].lower() != host.lower():
                return await self._refuse(send, 403, "bad_origin", "cross-origin request refused")
            if origin == "null":
                return await self._refuse(send, 403, "bad_origin", "cross-origin request refused")
        if path.startswith("/api") and h.get("sec-fetch-site") == "cross-site":
            return await self._refuse(send, 403, "bad_origin", "cross-site request refused")
        clen = h.get("content-length")
        has_body = (clen not in (None, "0")) or "transfer-encoding" in h
        if method in ("POST", "PUT", "PATCH", "DELETE") and has_body:
            if not h.get("content-type", "").lower().startswith("application/json"):
                return await self._refuse(
                    send, 415, "unsupported_media_type", "send Content-Type: application/json"
                )
        if clen and clen.isdigit() and int(clen) > MAX_BODY:
            return await self._refuse(send, 413, "too_large", f"body over {MAX_BODY} bytes")

        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > MAX_BODY:
                    raise _TooLarge()
            return msg

        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await self._refuse(send, 413, "too_large", f"body over {MAX_BODY} bytes")


class _TooLarge(Exception):
    pass


def create_app(
    db_path: str | Path = ":memory:",
    providers: ProviderSet | None = None,
    seed_tenants: bool = True,
    admin_token: str | None = None,
    allowed_hosts: frozenset[str] | set[str] | None | str = "loopback",
) -> FastAPI:
    """``admin_token``: required on /api when set (default: the LCR_ADMIN_TOKEN variable).
    ``allowed_hosts``: Host header values answered ("loopback" = localhost only, None = any)."""
    store = Store(db_path)
    core = Core(store, providers)
    sim = Simulator(core)
    if seed_tenants:
        core.seed_tenants()
    token = admin_token if admin_token is not None else os.environ.get("LCR_ADMIN_TOKEN", "")
    if allowed_hosts == "loopback":
        extra = {h.strip().lower() for h in os.environ.get("LCR_ALLOWED_HOSTS", "").split(",")}
        hosts = None if "*" in extra else frozenset(LOOPBACK_HOSTS | (extra - {""}))
    else:
        hosts = None if allowed_hosts is None else frozenset(h.lower() for h in allowed_hosts)
    fails = Throttle()

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "?"

    def admin(request: Request, x_admin_token: str = Header(default="")) -> None:
        if not token:
            return
        who = client_of(request)
        if fails.blocked(who):
            raise HTTPException(429, "too many failed attempts; wait a minute")
        if not hmac.compare_digest(x_admin_token.encode(), token.encode()):
            fails.fail(who)
            raise HTTPException(401, "admin token required (X-Admin-Token)")

    app = FastAPI(title="LLM Control Room", version=VERSION)
    app.state.core, app.state.sim = core, sim
    app.state.admin_token = token
    app.add_middleware(SecurityMiddleware, allowed_hosts=hosts)

    async def jbody(request: Request, optional: bool = False) -> dict:
        raw = await request.body()
        if not raw.strip():
            if optional:
                return {}
            raise ValueError("a JSON object body is required")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("the body must be a JSON object")
        return body

    @app.exception_handler(GatewayError)
    async def _gw(_: Request, exc: GatewayError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(KeyError)
    @app.exception_handler(TypeError)
    @app.exception_handler(ReleaseError)
    @app.exception_handler(TenantError)
    @app.exception_handler(ValueError)
    async def _bad(_: Request, exc: Exception):
        return JSONResponse(
            {"error": {"code": "bad_request", "message": str(exc)}}, status_code=400
        )

    def since(hours: float) -> float:
        return core.clock() - hours * 3600

    # ================================================================== gateway (OpenAI-compatible)

    @app.get("/v1/models")
    def v1_models(request: Request, authorization: str = Header(default="")):
        tenant = core.tenants.get(_auth(request, authorization)) or {}
        allowed = tenant.get("allowed_models") or []
        data = [{"id": "auto", "object": "model", "owned_by": "llm-control-room"}]
        data += [
            {"id": m.id, "object": "model", "owned_by": m.provider}
            for m in core.providers.models()
            if not allowed or m.id in allowed
        ]
        # a release is only listed when the model it serves is one this tenant may use
        for r in core.releases.list():
            served = {v["model"] for v in r["versions"]}
            if not allowed or served <= {*allowed, "auto"}:
                data.append({"id": r["name"], "object": "model", "owned_by": "release"})
        return {"object": "list", "data": data}

    def _auth(request: Request, authorization: str) -> str:
        who = client_of(request)
        if fails.blocked(who):
            raise GatewayError(429, "too_many_failures", "too many invalid API keys; wait a minute")
        key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        tenant = core.tenants.authenticate(key) if key else None
        if not tenant:
            fails.fail(who)
            raise GatewayError(
                401, "invalid_api_key", "missing or invalid API key (Authorization: Bearer lcr-...)"
            )
        return tenant

    @app.get("/v1/usage")
    def v1_usage(request: Request, authorization: str = Header(default="")):
        """What the calling tenant (and only it) has spent against its budget and rate limit."""
        name = _auth(request, authorization)
        t = core.tenants.get(name)
        now = core.clock()
        spent = store.one(
            "SELECT COALESCE(SUM(usd),0) AS s, COUNT(*) AS n FROM calls WHERE tenant=? "
            "AND at>? AND at<=?",
            (name, now - t["budget_window_s"], now),
        )
        recent = store.one(
            "SELECT COUNT(*) AS n FROM calls WHERE tenant=? AND at>? AND at<=? AND shadow=0 "
            "AND error_kind NOT IN ('rate_limited','budget','blocked')",
            (name, now - 60, now),
        )["n"]
        return {
            "tenant": name,
            "budget_usd": t["budget_usd"],
            "window_s": t["budget_window_s"],
            "spent_usd": round(spent["s"], 6),
            "remaining_usd": round(max(0.0, t["budget_usd"] - spent["s"]), 6),
            "calls_in_window": spent["n"],
            "rpm": t["rpm"],
            "requests_last_minute": recent,
        }

    @app.post("/v1/chat/completions")
    async def v1_chat(
        request: Request,
        authorization: str = Header(default=""),
        x_lcr_feature: str = Header(default=""),
    ):
        tenant = _auth(request, authorization)
        try:
            body = await jbody(request)
            ext = body.get("lcr") or {}
            if not isinstance(ext, dict):
                raise ValueError("lcr must be an object")
            model = body.get("model") or "auto"
            if not isinstance(model, str):
                raise ValueError("model must be a string")
            req = GatewayRequest(
                tenant=tenant,
                messages=body.get("messages") or [],
                model=model,
                max_tokens=int(body.get("max_tokens") or 512),
                temperature=float(body.get("temperature") or 0),
                feature=x_lcr_feature or str(ext.get("feature") or "default"),
                context=ext.get("context") or "",
                use_cache=bool(ext.get("use_cache", True)),
                session_key=str(ext.get("session") or body.get("user") or "")[:200],
                source="api",
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise GatewayError(400, "bad_request", f"invalid request: {exc}") from exc
        # the gateway does blocking work (a real provider call can take seconds): keep it off the
        # event loop so one slow upstream cannot freeze every other tenant and the admin UI
        res = await run_in_threadpool(core.gateway.handle, req)
        payload = {
            "id": res["id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": res["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": res["text"]},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                k: res["usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
            "lcr": {
                "cached": res["cached"],
                "usd": res["usage"]["usd"],
                "saved_usd": res["usage"]["saved_usd"],
                "difficulty": res["route"]["difficulty"],
                "route": res["route"]["reason"],
                "fallback_used": res["fallback_used"],
                "redactions": res["redactions"],
                "release": res["release"],
                "version": res["version"],
                "latency_ms": res["latency_ms"],
            },
        }
        headers = {
            "x-lcr-model": res["model"],
            "x-lcr-cache": "hit" if res["cached"] else "miss",
            "x-lcr-cost-usd": f"{res['usage']['usd']:.8f}",
            "x-lcr-trace": res["trace"],
        }
        if body.get("stream"):

            def events():
                text = res["text"]
                for i in range(0, max(len(text), 1), 24):
                    chunk = {
                        "id": res["id"],
                        "object": "chat.completion.chunk",
                        "model": res["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text[i : i + 24]},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                end = {
                    "id": res["id"],
                    "object": "chat.completion.chunk",
                    "model": res["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
        return JSONResponse(payload, headers=headers)

    # ================================================================== meta

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION}

    @app.get("/api/meta", dependencies=[Depends(admin)])
    def meta():
        return {
            "version": VERSION,
            "models": [m.to_dict() for m in core.providers.models()],
            "providers": [p.name for p in core.providers.providers],
            "real_providers": [p.name for p in core.providers.providers if p.name != "mock"],
            "demo_keys": {t: demo_key(t) for t in DEMO_TENANTS},
            "sim_scenarios": SIM_SCENARIOS,
            "agent_scenarios": core.runner.scenarios(),
            "sandbox_profiles": sandbox.profile_list(),
            "faults": core.providers.mock.faults,
            "admin_token_required": bool(token),
            "now": core.clock(),
            "settings": {
                "baseline_model": core.gateway.setting("baseline_model", ""),
                "cache_ttl_s": core.gateway.setting("cache_ttl_s", 3600),
            },
            "calls": store.one("SELECT COUNT(*) AS n FROM calls")["n"],
            "simulated_calls": store.one("SELECT COUNT(*) AS n FROM calls WHERE source='sim'")["n"],
        }

    @app.put("/api/settings", dependencies=[Depends(admin)])
    async def put_settings(request: Request):
        body = await jbody(request)
        if "baseline_model" in body:
            m = body["baseline_model"]
            if m and not core.providers.info(m):
                raise ValueError(f"unknown model {m!r}")
            store.kv_set("setting:baseline_model", m)
        if "cache_ttl_s" in body:
            ttl = float(body["cache_ttl_s"])
            if not math.isfinite(ttl) or ttl <= 0:
                raise ValueError("cache_ttl_s must be positive")
            store.kv_set("setting:cache_ttl_s", ttl)
        return meta()["settings"]

    @app.post("/api/cache/clear", dependencies=[Depends(admin)])
    def clear_cache():
        core.gateway.cache.clear()
        return {"cleared": True}

    @app.get("/api/faults", dependencies=[Depends(admin)])
    def get_faults():
        return core.providers.mock.faults

    @app.post("/api/faults", dependencies=[Depends(admin)])
    async def set_fault(request: Request):
        b = await jbody(request)
        if b.get("clear"):
            core.providers.mock.clear_faults()
        else:
            model = b.get("model", "")
            if not any(m.id == model and m.mock for m in core.providers.models()):
                raise ValueError("faults can only be injected into mock models")
            er, lm = float(b.get("error_rate", 0)), float(b.get("latency_mult", 1))
            if not 0 <= er <= 1 or not 0.1 <= lm <= 50:
                raise ValueError("error_rate must be 0-1 and latency_mult 0.1-50")
            core.providers.mock.set_fault(model, error_rate=er, latency_mult=lm)
        return core.providers.mock.faults

    # ================================================================== tenants

    @app.get("/api/tenants", dependencies=[Depends(admin)])
    def tenants():
        out = []
        for t in core.tenants.list():
            t["keys"] = core.tenants.keys(t["name"])
            spent = store.one(
                "SELECT COALESCE(SUM(usd),0) AS s, COUNT(*) AS n FROM calls WHERE tenant=? "
                "AND shadow=0 AND at>? AND at<=?",
                (t["name"], core.clock() - t["budget_window_s"], core.clock()),
            )
            t["spent_usd"], t["calls_in_window"] = round(spent["s"], 6), spent["n"]
            out.append(t)
        return out

    @app.post("/api/tenants", dependencies=[Depends(admin)])
    async def create_tenant(request: Request):
        b = await jbody(request)
        t = core.tenants.create(b.get("name", ""), **{k: v for k, v in b.items() if k != "name"})
        t["first_key"] = core.tenants.add_key(t["name"], "first key")
        return t

    @app.put("/api/tenants/{name}", dependencies=[Depends(admin)])
    async def update_tenant(name: str, request: Request):
        return core.tenants.update(name, **await jbody(request))

    @app.delete("/api/tenants/{name}", dependencies=[Depends(admin)])
    def delete_tenant(name: str):
        core.tenants.delete(name)
        return {"deleted": name}

    @app.post("/api/tenants/{name}/keys", dependencies=[Depends(admin)])
    async def new_key(name: str, request: Request):
        b = await jbody(request, optional=True)
        return core.tenants.add_key(name, b.get("label", ""))

    @app.delete("/api/keys/{key_id}", dependencies=[Depends(admin)])
    def revoke_key(key_id: int):
        core.tenants.revoke(key_id)
        return {"revoked": key_id}

    # ================================================================== playground + routing

    @app.post("/api/playground", dependencies=[Depends(admin)])
    async def playground(request: Request):
        b = await jbody(request)
        msgs = ([{"role": "system", "content": b["system"]}] if b.get("system") else []) + [
            {"role": "user", "content": b.get("prompt", "")}
        ]
        req = GatewayRequest(
            tenant=b.get("tenant", "acme"),
            messages=msgs,
            model=str(b.get("model") or "auto"),
            max_tokens=int(b.get("max_tokens") or 256),
            feature=str(b.get("feature") or "playground"),
            context=str(b.get("context") or "")[:MAX_CHARS],
            use_cache=bool(b.get("use_cache", True)),
            session_key=b.get("session", ""),
            source="playground",
            dry_run=bool(b.get("dry_run")),
        )
        return await run_in_threadpool(core.gateway.handle, req)

    @app.get("/api/routing", dependencies=[Depends(admin)])
    def routing(hours: float = 24.0):
        rows = obs.fetch(store, since=since(hours))
        baseline = core.gateway.setting("baseline_model", "")
        return {
            "report": obs.routing_report(rows),
            "frontier": obs.frontier(rows, core.providers.models(), baseline),
            "baseline_model": baseline or max(core.providers.models(), key=lambda m: m.usd_out).id,
        }

    # ================================================================== observability

    @app.get("/api/obs", dependencies=[Depends(admin)])
    def observability(hours: float = 24.0, tenant: str = "", feature: str = ""):
        rows = obs.fetch(store, since=since(hours), tenant=tenant, feature=feature)
        return {
            "summary": obs.summarise(rows),
            "series": obs.timeseries(rows, 24),
            "by_tenant": obs.group_by(rows, "tenant"),
            "by_feature": obs.group_by(rows, "feature"),
            "by_model": obs.group_by([r for r in rows if r["model"]], "model"),
            "drift": obs.drift(rows),
            "alerts": core.alerts.list(20),
        }

    @app.post("/api/alerts/evaluate", dependencies=[Depends(admin)])
    def evaluate_alerts(window_calls: int = 60):
        return {"fired": core.alerts.evaluate(max(30, min(window_calls, 1000)))}

    def _cell(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    @app.get("/api/export/calls.csv", dependencies=[Depends(admin)])
    def export_calls(hours: float = 24.0, tenant: str = "", include_simulated: bool = False):
        """Per-call cost and latency for chargeback. Never contains prompt or answer text."""
        cols = (
            "at",
            "trace",
            "tenant",
            "feature",
            "release",
            "version",
            "requested",
            "model",
            "provider",
            "route_reason",
            "prompt_tokens",
            "completion_tokens",
            "usd",
            "baseline_usd",
            "latency_ms",
            "cached",
            "fallback_used",
            "error_kind",
            "shadow",
            "source",
        )
        q, a = f"SELECT {','.join(cols)} FROM calls WHERE at>? AND source!='sim'", [since(hours)]
        if include_simulated:
            q = q.replace(" AND source!='sim'", "")
        if tenant:
            q, a = q + " AND tenant=?", a + [tenant]
        rows = store.all(q + " ORDER BY id DESC LIMIT 100000", a)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([_cell(r[c]) for c in cols])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="llm-control-room-calls.csv"'},
        )

    @app.get("/api/calls", dependencies=[Depends(admin)])
    def calls(limit: int = 60, tenant: str = "", errors_only: bool = False, shadow: bool = False):
        q, a = "SELECT * FROM calls WHERE shadow=?", [int(shadow)]
        if tenant:
            q, a = q + " AND tenant=?", a + [tenant]
        if errors_only:
            q += " AND (error!='' OR fallback_used=1)"
        rows = store.all(q + " ORDER BY id DESC LIMIT ?", [*a, max(1, min(limit, 500))])
        for r in rows:
            r["attempts"], r["redactions"] = json.loads(r["attempts"]), json.loads(r["redactions"])
        return rows

    # ================================================================== releases

    @app.get("/api/releases", dependencies=[Depends(admin)])
    def releases():
        return [
            {
                **r,
                "window": {
                    v["version"]: core.releases.window_stats(r["name"], v["version"])
                    for v in r["versions"]
                },
            }
            for r in core.releases.list()
        ]

    @app.post("/api/releases", dependencies=[Depends(admin)])
    async def create_release(request: Request):
        b = await jbody(request)
        return core.releases.create(
            b.get("name", ""),
            model=b.get("model", "auto"),
            system_prompt=b.get("system_prompt", ""),
            note=b.get("note", ""),
            slo=b.get("slo"),
        )

    @app.delete("/api/releases/{name}", dependencies=[Depends(admin)])
    def delete_release(name: str):
        core.releases.delete(name)
        return {"deleted": name}

    @app.get("/api/releases/{name}", dependencies=[Depends(admin)])
    def release(name: str):
        r = core.releases.get(name) if core.releases.exists(name) else None
        if r is None:
            raise HTTPException(404, "no such release")
        r["window"] = {
            v["version"]: core.releases.window_stats(name, v["version"]) for v in r["versions"]
        }
        r["analysis"] = core.releases.analysis(name)
        r["events"] = core.releases.events(name, 40)
        r["check"] = core.releases.last_check.get(name)
        return r

    @app.post("/api/releases/{name}/versions", dependencies=[Depends(admin)])
    async def add_version(name: str, request: Request):
        b = await jbody(request)
        return core.releases.add_version(
            name,
            model=b.get("model", "auto"),
            system_prompt=b.get("system_prompt", ""),
            note=b.get("note", ""),
        )

    @app.post("/api/releases/{name}/canary", dependencies=[Depends(admin)])
    async def canary(name: str, request: Request):
        b = await jbody(request)
        return core.releases.start_canary(
            name, int(b["version"]), float(b.get("traffic", 0.1)), b.get("mode", "canary")
        )

    @app.post("/api/releases/{name}/traffic", dependencies=[Depends(admin)])
    async def traffic(name: str, request: Request):
        return core.releases.set_traffic(name, float((await jbody(request))["traffic"]))

    @app.post("/api/releases/{name}/shadow", dependencies=[Depends(admin)])
    async def shadow(name: str, request: Request):
        b = await jbody(request)
        if b.get("remove"):
            return core.releases.remove_shadow(name, int(b["version"]))
        return core.releases.add_shadow(name, int(b["version"]))

    @app.post("/api/releases/{name}/promote", dependencies=[Depends(admin)])
    async def promote(name: str, request: Request):
        return core.releases.promote(name, int((await jbody(request))["version"]))

    @app.post("/api/releases/{name}/rollback", dependencies=[Depends(admin)])
    def rollback(name: str):
        return core.releases.rollback(name, reason="manual rollback")

    @app.put("/api/releases/{name}/slo", dependencies=[Depends(admin)])
    async def slo(name: str, request: Request):
        b = await jbody(request)
        return core.releases.set_slo(name, b.get("slo", {}), b.get("auto_rollback"))

    # ================================================================== agents

    @app.get("/api/runs", dependencies=[Depends(admin)])
    def runs():
        return core.runner.list()

    @app.post("/api/runs", dependencies=[Depends(admin)])
    async def start_run(request: Request):
        b = await jbody(request)
        if b.get("profile") == "subprocess" and b.get("allow_unsafe") is not True:
            raise ValueError("the subprocess profile needs allow_unsafe: true")
        return core.runner.start(
            b.get("tenant", "initech"),
            b.get("scenario", "research"),
            b.get("goal", ""),
            b.get("limits"),
            b.get("profile", "restricted"),
            b.get("ceiling", "write"),
        )

    @app.get("/api/runs/{rid}", dependencies=[Depends(admin)])
    def get_run(rid: str):
        try:
            return core.runner.get(rid)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/runs/{rid}/events", dependencies=[Depends(admin)])
    def run_events(rid: str, after: int = 0):
        return {"events": core.runner.events(rid, after), "run": get_run(rid)}

    @app.post("/api/runs/{rid}/approve", dependencies=[Depends(admin)])
    def approve(rid: str):
        return core.runner.decide(rid, True)

    @app.post("/api/runs/{rid}/deny", dependencies=[Depends(admin)])
    def deny(rid: str):
        return core.runner.decide(rid, False)

    @app.post("/api/runs/{rid}/cancel", dependencies=[Depends(admin)])
    def cancel(rid: str):
        core.runner.stop(rid)
        return {"cancelled": rid}

    # ================================================================== sandbox

    @app.get("/api/sandbox/profiles", dependencies=[Depends(admin)])
    def sandbox_profiles():
        return sandbox.profile_list()

    @app.post("/api/sandbox/run", dependencies=[Depends(admin)])
    async def sandbox_run(request: Request):
        b = await jbody(request)
        profile = b.get("profile", "restricted")
        if profile == "subprocess" and b.get("allow_unsafe") is not True:
            raise ValueError(
                "the subprocess profile runs the code with your own file and network access; "
                "pass allow_unsafe: true to confirm"
            )
        return await run_in_threadpool(
            lambda: sandbox.run_code(
                str(b.get("code", "")), profile, wall_seconds=float(b.get("wall_seconds", 5))
            )
        )

    @app.post("/api/sandbox/probe", dependencies=[Depends(admin)])
    async def sandbox_probe(request: Request):
        b = await jbody(request, optional=True)
        return await run_in_threadpool(sandbox.probe, b.get("profiles"))

    # ================================================================== simulator

    @app.post("/api/sim/run", dependencies=[Depends(admin)])
    async def sim_run(request: Request):
        b = await jbody(request)
        sim.live(False)
        n = int(b.get("n", 600))
        if not 1 <= n <= 5000:
            raise ValueError("n must be between 1 and 5000")
        return await run_in_threadpool(
            sim.run,
            b.get("scenario", "normal-day"),
            n,
            float(b.get("hours", 24)),
            int(b.get("seed", 1)),
        )

    @app.post("/api/sim/live", dependencies=[Depends(admin)])
    async def sim_live(request: Request):
        b = await jbody(request)
        return sim.live(bool(b.get("on")), float(b.get("rate", 2)))

    @app.get("/api/sim/live", dependencies=[Depends(admin)])
    def sim_live_status():
        return sim.live_status()

    @app.post("/api/reset", dependencies=[Depends(admin)])
    def reset():
        sim.live(False)
        store.wipe_traffic()
        for r in core.releases.list():
            core.releases.delete(r["name"])
        core.gateway.cache.clear()
        core.providers.mock.clear_faults()
        return {"reset": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


__all__ = ["create_app"]
