"""Agent runs under hard limits, with a live run log.

The idea is bounded-agent-runtime's: the thing being limited must not be the thing checking the
limit, so every ceiling (steps, tool calls, cost, wall time, repeated actions) lives in the
runner, is checked before each step, and raises rather than returns. Tools declare their own risk
tier; anything above the autonomous ceiling pauses the run for a human. Each reasoning step is a
real call through the gateway, so its cost is the gateway's cost for that tenant.

Planners are scripted scenarios (deterministic, no model needed) plus a free-form ``llm`` planner
that asks whatever model the gateway routes to. The scripted ones exist so every limit can be
shown firing on demand.
"""

from __future__ import annotations

import ast
import enum
import json
import math
import operator
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ._vendor.limits import Budget, BudgetExceeded
from .gateway import Gateway, GatewayError, GatewayRequest
from .guard import screen
from .sandbox import PROFILES, docker_available, docker_status, run_code
from .store import Store


class RiskTier(enum.IntEnum):
    READ = 0
    WRITE = 1
    EXTERNAL = 2
    IRREVERSIBLE = 3


class ToolError(RuntimeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    fn: Any
    tier: RiskTier = RiskTier.READ
    usd: float = 0.0


DOCS = [
    (
        "refund-policy",
        "Refunds are issued within 14 days of purchase for unused items. Refunds "
        "go back to the original payment method and take 5 to 7 business days to appear.",
    ),
    (
        "shipping",
        "Standard shipping takes 3 to 5 business days inside the country. Express "
        "shipping takes 1 to 2 business days and costs 12 dollars extra.",
    ),
    (
        "warranty",
        "Hardware carries a 24 month warranty. Water damage and physical impact are "
        "not covered by the warranty.",
    ),
    (
        "privacy",
        "Customer data is kept for 36 months after the last order, then deleted. "
        "Customers can request deletion at any time through support.",
    ),
    (
        "support-hours",
        "Support is available Monday to Friday from 9 in the morning to 6 in the "
        "evening. Weekend queries are answered on the next business day.",
    ),
    (
        "order-tracking",
        "Customers can track an order with the order number from the "
        "confirmation email. Tracking updates appear within 24 hours of dispatch.",
    ),
]
NOTES = {
    "customer-123": "Priya Nair asked about a refund for a damaged keyboard. Order 88231. "
    "Item arrived with a cracked case; photo attached."
}

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def calc(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            if isinstance(n.op, ast.Pow) and abs(ev(n.right)) > 64:
                raise ToolError("exponent too large")
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ToolError("only arithmetic is allowed")

    try:
        return ev(ast.parse(expr, mode="eval").body)
    except (SyntaxError, ZeroDivisionError) as exc:
        raise ToolError(f"bad expression: {exc}") from exc


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def search_docs(query: str) -> str:
    q = _stems(query)
    scored = sorted(((len(q & _stems(n + " " + t)), n, t) for n, t in DOCS), reverse=True)
    hits = [f"[{n}] {t}" for s, n, t in scored[:2] if s > 0]
    return "\n".join(hits) if hits else "no matching documents"


def build_tools(profile: str) -> dict[str, Tool]:
    def read_note(id: str) -> str:
        if id not in NOTES:
            raise ToolError(f"no note {id!r}")
        return NOTES[id]

    def slow_lookup(seconds: float = 0.5) -> str:
        time.sleep(min(float(seconds), 3.0))
        return "lookup finished"

    def code(source: str) -> str:
        r = run_code(source, profile, wall_seconds=5)
        if r["timed_out"]:
            raise ToolError("code timed out and was killed")
        out = (r["stdout"] + r["stderr"]).strip()
        if r["exit_code"] not in (0, None):
            raise ToolError(out[-400:] or f"exit code {r['exit_code']}")
        return out[-600:]

    tools = [
        Tool(
            "search_docs",
            "Search the support handbook",
            lambda query: search_docs(query),
            RiskTier.READ,
            0.002,
        ),
        Tool(
            "premium_search",
            "Paid web search",
            lambda query: f"3 results for {query!r}",
            RiskTier.READ,
            0.04,
        ),
        Tool("calculator", "Evaluate arithmetic", lambda expr: str(calc(expr)), RiskTier.READ),
        Tool("read_note", "Read a customer note", read_note, RiskTier.READ),
        Tool("slow_lookup", "A slow upstream lookup", slow_lookup, RiskTier.READ, 0.001),
        Tool(
            "send_email",
            "Send an email to a customer",
            lambda to, subject, body: f"email queued to {to}",
            RiskTier.EXTERNAL,
            0.001,
        ),
        Tool(
            "delete_records",
            "Delete rows from a table",
            lambda table, where: f"deleted rows from {table} where {where}",
            RiskTier.IRREVERSIBLE,
        ),
        # running code unsandboxed leaves the system, so it is gated; sandboxed code is a write
        Tool(
            "run_code",
            f"Run Python under the {profile} profile",
            code,
            RiskTier.EXTERNAL if profile == "subprocess" else RiskTier.WRITE,
            0.003,
        ),
    ]
    return {t.name: t for t in tools}


@dataclass
class Action:
    tool: str
    args: dict = field(default_factory=dict)
    why: str = ""

    def fingerprint(self) -> str:
        return f"{self.tool}:{json.dumps(self.args, sort_keys=True, default=str)}"


def _last_obs(history: list[dict]) -> str:
    return str(history[-1]["observation"]) if history else ""


def _denied(history: list[dict]) -> bool:
    return bool(history) and str(history[-1]["observation"]).startswith("DENIED")


CODE_STATS = (
    "import statistics\nxs = [12, 15, 11, 19, 14, 22, 13]\n"
    "print('mean', round(statistics.mean(xs), 2), 'median', statistics.median(xs))\n"
)
CODE_ESCAPE = (
    "import os\nprint('secret:', os.environ.get('LCR_DEMO_SECRET', os.environ.get('USERNAME', 'none')))\n"
    "print(open(r'C:\\Windows\\win.ini').read()[:60] if os.name == 'nt' else open('/etc/hostname').read())\n"
)


def plan_research(h, n, goal):
    steps = [
        Action("search_docs", {"query": "refund policy"}, "find the refund rules"),
        Action("search_docs", {"query": "shipping times"}, "find delivery times"),
        Action("calculator", {"expr": "14 * 24"}, "convert 14 days to hours"),
    ]
    return steps[n] if n < len(steps) else Action("finish", {"compose": True}, "enough evidence")


def plan_loop(h, n, goal):
    return Action("search_docs", {"query": "where is my order"}, "try again")


def plan_spend(h, n, goal):
    return Action("premium_search", {"query": f"competitor pricing {n}"}, "keep searching")


def plan_slow(h, n, goal):
    return Action("slow_lookup", {"seconds": 0.7 + n / 100}, "wait for the upstream")


def plan_chatty(h, n, goal):
    return Action("calculator", {"expr": f"{n} * {n + 1}"}, "one more calculation")


def plan_email(h, n, goal):
    if n == 0:
        return Action("read_note", {"id": "customer-123"}, "read the case")
    if n == 1:
        return Action(
            "send_email",
            {
                "to": "priya@example.com",
                "subject": "Your refund",
                "body": "Your refund for order 88231 is approved.",
            },
            "tell the customer",
        )
    return Action(
        "finish",
        {
            "answer": "Email not sent: a human declined it."
            if _denied(h)
            else "Refund email sent to the customer."
        },
    )


def plan_delete(h, n, goal):
    if n == 0:
        return Action(
            "delete_records",
            {"table": "tickets", "where": "status = 'closed'"},
            "clean up old tickets",
        )
    return Action(
        "finish",
        {"answer": "Deletion declined by a human." if _denied(h) else "Closed tickets deleted."},
    )


def plan_code(h, n, goal):
    if n == 0:
        return Action("run_code", {"source": CODE_STATS}, "compute the statistics")
    return Action("finish", {"answer": f"Result: {_last_obs(h)}"})


def plan_escape(h, n, goal):
    if n == 0:
        return Action("run_code", {"source": CODE_ESCAPE}, "inspect the environment")
    return Action("finish", {"answer": f"Code output: {_last_obs(h)[:200]}"})


SCENARIOS: dict[str, dict] = {
    "research": {
        "fn": plan_research,
        "title": "Research and answer",
        "goal": "What is the refund window and how long does shipping take?",
        "limits": {},
        "expect": "completes; final answer is grounded in the handbook",
    },
    "loop": {
        "fn": plan_loop,
        "title": "Stuck in a loop",
        "goal": "Find where my order is.",
        "limits": {},
        "expect": "stops on loop detection after 3 identical actions",
    },
    "spendthrift": {
        "fn": plan_spend,
        "title": "Runaway cost",
        "goal": "Compare every competitor's pricing.",
        "limits": {"max_usd": 0.2},
        "expect": "stops on the cost limit",
    },
    "slow": {
        "fn": plan_slow,
        "title": "Slow upstream",
        "goal": "Wait for the report.",
        "limits": {"max_seconds": 2.0},
        "expect": "stops on the time limit",
    },
    "chatty": {
        "fn": plan_chatty,
        "title": "Never finishes",
        "goal": "Keep calculating.",
        "limits": {"max_steps": 6, "max_repeats": 5},
        "expect": "stops on the step limit",
    },
    "email": {
        "fn": plan_email,
        "title": "Send an email (approval gate)",
        "goal": "Tell the customer in note customer-123 that the refund is approved.",
        "limits": {},
        "expect": "pauses for approval before send_email",
    },
    "delete": {
        "fn": plan_delete,
        "title": "Delete records (irreversible)",
        "goal": "Clean up closed tickets.",
        "limits": {},
        "expect": "pauses for approval: irreversible",
    },
    "code": {
        "fn": plan_code,
        "title": "Run generated code",
        "goal": "Summarise the sample numbers.",
        "limits": {},
        "expect": "runs under the chosen sandbox profile",
    },
    "code-escape": {
        "fn": plan_escape,
        "title": "Generated code reads the host",
        "goal": "Inspect the machine.",
        "limits": {},
        "expect": "blocked under restricted/hardened; paused for approval under subprocess",
    },
    "llm": {
        "fn": None,
        "title": "Free-form (asks a model)",
        "goal": "Say hello in one sentence.",
        "limits": {},
        "expect": "uses whatever model the gateway routes to",
    },
}

DEFAULT_LIMITS = {
    "max_steps": 12,
    "max_seconds": 30.0,
    "max_usd": 0.25,
    "max_tool_calls": 20,
    "max_repeats": 3,
}
LIMIT_CAPS = {
    "max_steps": 100,
    "max_seconds": 600.0,
    "max_usd": 10.0,
    "max_tool_calls": 200,
    "max_repeats": 50,
}
MAX_RUNNING = 8  # agent threads alive at once
CEILINGS = {"read": RiskTier.READ, "write": RiskTier.WRITE, "external": RiskTier.EXTERNAL}


class AgentRunner:
    def __init__(self, store: Store, gateway: Gateway) -> None:
        self.store, self.gateway = store, gateway
        self.pending: dict[str, dict] = {}
        self.cancel: set[str] = set()
        self.threads: dict[str, threading.Thread] = {}
        # runs left 'running' by a previous process can never finish
        self.store.run(
            "UPDATE runs SET status='interrupted' WHERE status IN ('running','awaiting_approval')"
        )

    # ------------------------------------------------------------------ api

    def scenarios(self) -> list[dict]:
        return [
            {
                "id": k,
                "title": v["title"],
                "goal": v["goal"],
                "limits": v["limits"],
                "expect": v["expect"],
            }
            for k, v in SCENARIOS.items()
        ]

    def start(
        self,
        tenant: str,
        scenario: str,
        goal: str = "",
        limits: dict | None = None,
        profile: str = "restricted",
        ceiling: str = "write",
        wait: bool = False,
    ) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        if profile not in PROFILES:
            raise ValueError(f"unknown sandbox profile {profile!r}")
        if profile == "hardened" and not docker_available():
            raise ValueError(f"the hardened profile is unavailable: {docker_status()[1]}")
        if ceiling not in CEILINGS:
            raise ValueError("ceiling must be read, write or external")
        if not self.gateway.tenants.get(tenant):
            raise ValueError(f"no tenant {tenant!r}")
        merged = {**DEFAULT_LIMITS, **SCENARIOS[scenario]["limits"], **(limits or {})}
        for k in merged:
            if k not in DEFAULT_LIMITS:
                raise ValueError(f"unknown limit {k!r}")
            v = float(merged[k])
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"{k} must be a positive number")
            if v > LIMIT_CAPS[k]:
                raise ValueError(f"{k} may not exceed {LIMIT_CAPS[k]:g}")
        self.threads = {k: t for k, t in self.threads.items() if t.is_alive()}
        if len(self.threads) >= MAX_RUNNING:
            raise ValueError(f"{MAX_RUNNING} runs are already in progress; wait or cancel one")
        rid = uuid.uuid4().hex[:10]
        # the goal is the only prompt-like text a run stores, so secrets and PII are removed first
        goal = str(goal).strip()[:2000] or SCENARIOS[scenario]["goal"]
        goal = screen(goal, redact_pii=True, check_injection=False).text
        self.store.run(
            "INSERT INTO runs(id, created, tenant, scenario, goal, status, limits, profile) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (rid, time.time(), tenant, scenario, goal, "running", json.dumps(merged), profile),
        )
        t = threading.Thread(
            target=self._run,
            daemon=True,
            args=(rid, tenant, scenario, goal, merged, profile, ceiling),
        )
        self.threads[rid] = t
        t.start()
        if wait:
            t.join(timeout=60)
        return self.get(rid)

    def decide(self, rid: str, approve: bool) -> dict:
        p = self.pending.get(rid)
        if not p:
            raise ValueError("this run is not waiting for approval")
        p["approve"] = approve
        p["event"].set()
        return {"run": rid, "approved": approve}

    def stop(self, rid: str) -> None:
        self.cancel.add(rid)
        if rid in self.pending:
            self.pending[rid]["approve"] = False
            self.pending[rid]["event"].set()

    def get(self, rid: str) -> dict:
        r = self.store.one("SELECT * FROM runs WHERE id=?", (rid,))
        if not r:
            raise ValueError(f"no run {rid!r}")
        r["limits"], r["result"] = json.loads(r["limits"]), json.loads(r["result"])
        if rid in self.pending:
            r["pending"] = {
                k: v for k, v in self.pending[rid].items() if k in ("tool", "args", "tier")
            }
        return r

    def list(self, limit: int = 40) -> list[dict]:
        rows = self.store.all("SELECT id FROM runs ORDER BY created DESC LIMIT ?", (limit,))
        return [self.get(r["id"]) for r in rows]

    def events(self, rid: str, after: int = 0) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq", (rid, after)
        )
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # ------------------------------------------------------------------ the loop

    def _log(self, rid: str, seq: list[int], kind: str, **data) -> None:
        seq[0] += 1
        self.store.run(
            "INSERT INTO run_events(run_id, seq, at, kind, data) VALUES(?,?,?,?,?)",
            (rid, seq[0], time.time(), kind, json.dumps(data, default=str)),
        )

    def _finish(self, rid, seq, status, budget, **extra) -> None:
        result = {
            "steps": budget.steps,
            "tool_calls": budget.tool_calls,
            "usd": round(budget.usd, 6),
            "seconds": round(budget.elapsed, 2),
            **extra,
        }
        self._log(rid, seq, "finish", status=status, **result)
        self.store.run(
            "UPDATE runs SET status=?, result=? WHERE id=?", (status, json.dumps(result), rid)
        )

    def _think(self, tenant, scenario, goal, history, budget) -> dict | None:
        prompt = (
            f"Goal: {goal}\nObservations so far: {len(history)}. "
            f"Last: {_last_obs(history)[:160]}\nWhat is the next step?"
        )
        try:
            r = self.gateway.handle(
                GatewayRequest(
                    tenant=tenant,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=60,
                    feature=f"agent-{scenario}",
                    source="agent",
                )
            )
        except GatewayError as exc:
            return {"error": exc.message}
        budget.record_cost(r["usage"]["usd"])
        return r

    def _run(self, rid, tenant, scenario, goal, limits, profile, ceiling_name) -> None:
        seq = [0]
        budget = Budget(
            max_steps=int(limits["max_steps"]),
            max_seconds=float(limits["max_seconds"]),
            max_usd=float(limits["max_usd"]),
            max_tool_calls=int(limits["max_tool_calls"]),
            max_repeats=int(limits["max_repeats"]),
        )
        ceiling = CEILINGS[ceiling_name]
        tools = build_tools(profile)
        history: list[dict] = []
        planner = SCENARIOS[scenario]["fn"]
        self._log(
            rid,
            seq,
            "start",
            goal=goal,
            scenario=scenario,
            profile=profile,
            ceiling=ceiling_name,
            limits=limits,
        )
        try:
            while True:
                if rid in self.cancel:
                    return self._finish(rid, seq, "cancelled", budget)
                try:
                    budget.check()
                except BudgetExceeded as stop:
                    self._log(
                        rid, seq, "budget_stop", limit=stop.kind, max=stop.limit, used=stop.used
                    )
                    return self._finish(
                        rid, seq, "budget_stopped", budget, reason=str(stop), limit=stop.kind
                    )
                thought = self._think(tenant, scenario, goal, history, budget)
                if thought and "error" in thought:
                    self._log(rid, seq, "error", message=thought["error"])
                    return self._finish(rid, seq, "failed", budget, reason=thought["error"])
                n = budget.steps
                if planner is None:
                    action = self._llm_action(thought)
                else:
                    action = planner(history, n, goal)
                if thought:
                    self._log(
                        rid,
                        seq,
                        "thought",
                        text=thought["text"][:240],
                        model=thought["model"],
                        usd=thought["usage"]["usd"],
                        saved=thought["usage"]["saved_usd"],
                    )
                try:
                    budget.record_step(action.fingerprint())
                except BudgetExceeded as stop:
                    self._log(
                        rid,
                        seq,
                        "budget_stop",
                        limit=stop.kind,
                        max=stop.limit,
                        used=stop.used,
                        action=action.tool,
                    )
                    return self._finish(
                        rid, seq, "budget_stopped", budget, reason=str(stop), limit=stop.kind
                    )
                self._log(
                    rid,
                    seq,
                    "step",
                    n=budget.steps,
                    tool=action.tool,
                    args=action.args,
                    why=action.why,
                )
                if action.tool == "finish":
                    answer = action.args.get("answer") or self._compose(
                        tenant, goal, history, rid, seq
                    )
                    return self._finish(rid, seq, "completed", budget, answer=answer)
                tool = tools.get(action.tool)
                budget.record_tool_call()
                if tool is None:
                    self._log(rid, seq, "tool_error", tool=action.tool, error="no such tool")
                    history.append({"action": action.tool, "observation": "ERROR: no such tool"})
                    continue
                budget.record_cost(tool.usd)
                approved = tool.tier <= ceiling
                if not approved:
                    verdict = self._await_approval(rid, seq, tool, action, budget)
                    if verdict is None:
                        return self._finish(rid, seq, "failed", budget, reason="approval timed out")
                    if rid in self.cancel:
                        return self._finish(rid, seq, "cancelled", budget)
                    if not verdict:
                        history.append(
                            {
                                "action": tool.name,
                                "args": action.args,
                                "observation": "DENIED by a human reviewer",
                            }
                        )
                        self._log(rid, seq, "approval", decision="denied", tool=tool.name)
                        if scenario in ("email", "delete", "code-escape"):
                            continue
                        return self._finish(
                            rid, seq, "denied", budget, reason=f"{tool.name} denied"
                        )
                    self._log(rid, seq, "approval", decision="approved", tool=tool.name)
                try:
                    obs = tool.fn(**action.args)
                    self._log(
                        rid,
                        seq,
                        "tool_call",
                        tool=tool.name,
                        ok=True,
                        observation=str(obs)[:500],
                        usd=tool.usd,
                    )
                    history.append({"action": tool.name, "args": action.args, "observation": obs})
                except TypeError as exc:
                    self._tool_error(rid, seq, tool, f"wrong arguments: {exc}", history, action)
                except Exception as exc:
                    self._tool_error(rid, seq, tool, str(exc), history, action)
        except Exception as exc:  # never leave a run stuck at 'running'
            self._log(rid, seq, "error", message=f"{type(exc).__name__}: {exc}")
            self._finish(rid, seq, "failed", budget, reason=str(exc))
        finally:
            self.pending.pop(rid, None)

    def _tool_error(self, rid, seq, tool, msg, history, action) -> None:
        self._log(rid, seq, "tool_error", tool=tool.name, error=msg[:400])
        history.append(
            {"action": tool.name, "args": action.args, "observation": f"ERROR: {msg[:300]}"}
        )

    def _await_approval(self, rid, seq, tool, action, budget) -> bool | None:
        ev = threading.Event()
        self.pending[rid] = {
            "event": ev,
            "approve": None,
            "tool": tool.name,
            "args": action.args,
            "tier": tool.tier.name,
        }
        self.store.run("UPDATE runs SET status='awaiting_approval' WHERE id=?", (rid,))
        self._log(
            rid, seq, "approval_required", tool=tool.name, tier=tool.tier.name, args=action.args
        )
        t0 = time.monotonic()
        ok = ev.wait(timeout=600)
        budget.started_at += time.monotonic() - t0  # waiting on a human is not agent time
        decision = self.pending.pop(rid)["approve"] if ok else None
        self.store.run("UPDATE runs SET status='running' WHERE id=?", (rid,))
        return decision

    def _compose(self, tenant, goal, history, rid, seq) -> str:
        context = " ".join(str(h["observation"]) for h in history if h["action"] == "search_docs")
        try:
            r = self.gateway.handle(
                GatewayRequest(
                    tenant=tenant,
                    messages=[{"role": "user", "content": goal}],
                    context=context,
                    feature="agent-answer",
                    max_tokens=200,
                    source="agent",
                )
            )
        except GatewayError as exc:
            return f"could not compose an answer: {exc.message}"
        self._log(
            rid,
            seq,
            "answer",
            text=r["text"],
            model=r["model"],
            grounding=r["grounding"],
            usd=r["usage"]["usd"],
        )
        return r["text"]

    @staticmethod
    def _llm_action(thought: dict | None) -> Action:
        text = (thought or {}).get("text", "")
        m = re.search(r"ACTION:\s*(\w+)\s*(\{.*\})?", text)
        if m and m.group(1) != "finish":
            try:
                return Action(m.group(1), json.loads(m.group(2) or "{}"), "model chose")
            except ValueError:
                pass
        return Action("finish", {"answer": text or "no answer"}, "model answered")
