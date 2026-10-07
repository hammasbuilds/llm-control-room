"""HTTP surface: the OpenAI-compatible gateway on /v1 and the admin API on /api."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import obs, sandbox
from .core import DEMO_TENANTS, Core, demo_key
from .gateway import GatewayError, GatewayRequest
from .providers import ProviderSet
from .releases import ReleaseError
from .simulator import SCENARIOS as SIM_SCENARIOS
from .simulator import Simulator
from .store import Store
from .tenants import TenantError

STATIC = Path(__file__).parent / "static"
VERSION = "0.1.0"


def create_app(
    db_path: str | Path = ":memory:",
    providers: ProviderSet | None = None,
    seed_tenants: bool = True,
) -> FastAPI:
    store = Store(db_path)
    core = Core(store, providers)
    sim = Simulator(core)
    if seed_tenants:
        core.seed_tenants()
    token = os.environ.get("LCR_ADMIN_TOKEN", "")

    def admin(x_admin_token: str = Header(default="")) -> None:
        if token and x_admin_token != token:
            raise HTTPException(401, "admin token required (X-Admin-Token)")

    app = FastAPI(title="LLM Control Room", version=VERSION)
    app.state.core, app.state.sim = core, sim

    @app.exception_handler(GatewayError)
    async def _gw(_: Request, exc: GatewayError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

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
    def v1_models(authorization: str = Header(default="")):
        _auth(authorization)
        data = [{"id": "auto", "object": "model", "owned_by": "llm-control-room"}]
        data += [
            {"id": m.id, "object": "model", "owned_by": m.provider} for m in core.providers.models()
        ]
        data += [
            {"id": r["name"], "object": "model", "owned_by": "release"}
            for r in core.releases.list()
        ]
        return {"object": "list", "data": data}

    def _auth(authorization: str) -> str:
        key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        tenant = core.tenants.authenticate(key) if key else None
        if not tenant:
            raise GatewayError(
                401, "invalid_api_key", "missing or invalid API key (Authorization: Bearer lcr-...)"
            )
        return tenant

    @app.post("/v1/chat/completions")
    async def v1_chat(
        request: Request,
        authorization: str = Header(default=""),
        x_lcr_feature: str = Header(default=""),
    ):
        tenant = _auth(authorization)
        try:
            body = await request.json()
        except ValueError as exc:
            raise GatewayError(400, "bad_request", "body must be JSON") from exc
        ext = body.get("lcr") or {}
        req = GatewayRequest(
            tenant=tenant,
            messages=body.get("messages") or [],
            model=body.get("model") or "auto",
            max_tokens=int(body.get("max_tokens") or 512),
            temperature=float(body.get("temperature") or 0),
            feature=x_lcr_feature or ext.get("feature") or "default",
            context=ext.get("context", ""),
            use_cache=bool(ext.get("use_cache", True)),
            session_key=str(ext.get("session") or body.get("user") or ""),
            source="api",
        )
        res = core.gateway.handle(req)
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
        body = await request.json()
        if "baseline_model" in body:
            m = body["baseline_model"]
            if m and not core.providers.info(m):
                raise ValueError(f"unknown model {m!r}")
            store.kv_set("setting:baseline_model", m)
        if "cache_ttl_s" in body:
            ttl = float(body["cache_ttl_s"])
            if ttl <= 0:
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
        b = await request.json()
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
        b = await request.json()
        t = core.tenants.create(b.get("name", ""), **{k: v for k, v in b.items() if k != "name"})
        t["first_key"] = core.tenants.add_key(t["name"], "first key")
        return t

    @app.put("/api/tenants/{name}", dependencies=[Depends(admin)])
    async def update_tenant(name: str, request: Request):
        return core.tenants.update(name, **await request.json())

    @app.delete("/api/tenants/{name}", dependencies=[Depends(admin)])
    def delete_tenant(name: str):
        core.tenants.delete(name)
        return {"deleted": name}

    @app.post("/api/tenants/{name}/keys", dependencies=[Depends(admin)])
    async def new_key(name: str, request: Request):
        b = await request.json() if (await request.body()) else {}
        return core.tenants.add_key(name, b.get("label", ""))

    @app.delete("/api/keys/{key_id}", dependencies=[Depends(admin)])
    def revoke_key(key_id: int):
        core.tenants.revoke(key_id)
        return {"revoked": key_id}

    # ================================================================== playground + routing

    @app.post("/api/playground", dependencies=[Depends(admin)])
    async def playground(request: Request):
        b = await request.json()
        msgs = ([{"role": "system", "content": b["system"]}] if b.get("system") else []) + [
            {"role": "user", "content": b.get("prompt", "")}
        ]
        req = GatewayRequest(
            tenant=b.get("tenant", "acme"),
            messages=msgs,
            model=b.get("model") or "auto",
            max_tokens=int(b.get("max_tokens") or 256),
            feature=b.get("feature") or "playground",
            context=b.get("context", ""),
            use_cache=bool(b.get("use_cache", True)),
            session_key=b.get("session", ""),
            source="playground",
            dry_run=bool(b.get("dry_run")),
        )
        return core.gateway.handle(req)

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

    @app.get("/api/calls", dependencies=[Depends(admin)])
    def calls(limit: int = 60, tenant: str = "", errors_only: bool = False, shadow: bool = False):
        q, a = "SELECT * FROM calls WHERE shadow=?", [int(shadow)]
        if tenant:
            q, a = q + " AND tenant=?", a + [tenant]
        if errors_only:
            q += " AND (error!='' OR fallback_used=1)"
        rows = store.all(q + " ORDER BY id DESC LIMIT ?", [*a, min(limit, 500)])
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
        b = await request.json()
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
        b = await request.json()
        return core.releases.add_version(
            name,
            model=b.get("model", "auto"),
            system_prompt=b.get("system_prompt", ""),
            note=b.get("note", ""),
        )

    @app.post("/api/releases/{name}/canary", dependencies=[Depends(admin)])
    async def canary(name: str, request: Request):
        b = await request.json()
        return core.releases.start_canary(
            name, int(b["version"]), float(b.get("traffic", 0.1)), b.get("mode", "canary")
        )

    @app.post("/api/releases/{name}/traffic", dependencies=[Depends(admin)])
    async def traffic(name: str, request: Request):
        return core.releases.set_traffic(name, float((await request.json())["traffic"]))

    @app.post("/api/releases/{name}/shadow", dependencies=[Depends(admin)])
    async def shadow(name: str, request: Request):
        b = await request.json()
        if b.get("remove"):
            return core.releases.remove_shadow(name, int(b["version"]))
        return core.releases.add_shadow(name, int(b["version"]))

    @app.post("/api/releases/{name}/promote", dependencies=[Depends(admin)])
    async def promote(name: str, request: Request):
        return core.releases.promote(name, int((await request.json())["version"]))

    @app.post("/api/releases/{name}/rollback", dependencies=[Depends(admin)])
    def rollback(name: str):
        return core.releases.rollback(name, reason="manual rollback")

    @app.put("/api/releases/{name}/slo", dependencies=[Depends(admin)])
    async def slo(name: str, request: Request):
        b = await request.json()
        return core.releases.set_slo(name, b.get("slo", {}), b.get("auto_rollback"))

    # ================================================================== agents

    @app.get("/api/runs", dependencies=[Depends(admin)])
    def runs():
        return core.runner.list()

    @app.post("/api/runs", dependencies=[Depends(admin)])
    async def start_run(request: Request):
        b = await request.json()
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
        b = await request.json()
        return sandbox.run_code(
            str(b.get("code", "")),
            b.get("profile", "restricted"),
            wall_seconds=float(b.get("wall_seconds", 5)),
        )

    @app.post("/api/sandbox/probe", dependencies=[Depends(admin)])
    async def sandbox_probe(request: Request):
        b = await request.json() if (await request.body()) else {}
        return sandbox.probe(b.get("profiles"))

    # ================================================================== simulator

    @app.post("/api/sim/run", dependencies=[Depends(admin)])
    async def sim_run(request: Request):
        b = await request.json()
        sim.live(False)
        return sim.run(
            b.get("scenario", "normal-day"),
            int(b.get("n", 600)),
            float(b.get("hours", 24)),
            int(b.get("seed", 1)),
        )

    @app.post("/api/sim/live", dependencies=[Depends(admin)])
    async def sim_live(request: Request):
        b = await request.json()
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
