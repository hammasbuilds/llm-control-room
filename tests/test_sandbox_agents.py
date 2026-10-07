import time

import pytest

from llm_control_room import sandbox
from llm_control_room.agents import ToolError, calc, search_docs


def test_profiles_listed_with_availability():
    names = {p["name"]: p for p in sandbox.profile_list()}
    assert names["subprocess"]["available"] and names["restricted"]["available"]
    assert "hardened" in names


def test_plain_code_runs_everywhere():
    for prof in ("subprocess", "restricted"):
        r = sandbox.run_code("print(6 * 7)", prof)
        assert r["stdout"].strip() == "42" and r["exit_code"] == 0 and not r["timed_out"]


def test_restricted_blocks_what_subprocess_allows(tmp_path):
    secret = tmp_path / "s.txt"
    secret.write_text("TOPSECRET")
    code = f"print(open(r'{secret}').read())"
    assert "TOPSECRET" in sandbox.run_code(code, "subprocess")["stdout"]
    r = sandbox.run_code(code, "restricted")
    assert "TOPSECRET" not in r["stdout"] and "PermissionError" in r["stderr"]


def test_restricted_scrubs_the_environment(monkeypatch):
    monkeypatch.setenv("LCR_TEST_SECRET", "abc123")
    code = "import os; print(os.environ.get('LCR_TEST_SECRET', 'absent'))"
    assert "abc123" in sandbox.run_code(code, "subprocess")["stdout"]
    assert sandbox.run_code(code, "restricted")["stdout"].strip() == "absent"


def test_restricted_blocks_processes_and_network_and_outside_writes(tmp_path):
    assert (
        "blocked"
        in sandbox.run_code("import subprocess; subprocess.run(['echo','x'])", "restricted")[
            "stderr"
        ]
    )
    assert (
        "blocked"
        in sandbox.run_code(
            "import socket; socket.create_connection(('127.0.0.1', 9), timeout=1)", "restricted"
        )["stderr"]
    )
    target = tmp_path / "out.txt"
    sandbox.run_code(f"open(r'{target}', 'w').write('x')", "restricted")
    assert not target.exists()


def test_restricted_allows_writes_inside_the_workdir():
    r = sandbox.run_code(
        "open('note.txt', 'w').write('hi'); print(open('note.txt').read())", "restricted"
    )
    assert r["stdout"].strip() == "hi"


def test_timeout_kills_a_runaway():
    t = time.time()
    r = sandbox.run_code("while True: pass", "restricted", wall_seconds=1)
    assert r["timed_out"] and time.time() - t < 8


def test_output_is_capped_and_the_process_killed():
    r = sandbox.run_code(
        "while True: print('x' * 4096)", "restricted", wall_seconds=10, output_bytes=20_000
    )
    assert r["output_truncated"] and len(r["stdout"]) <= 30_000 and not r["timed_out"]


def test_bad_arguments():
    with pytest.raises(ValueError):
        sandbox.run_code("1", "nope")
    with pytest.raises(ValueError):
        sandbox.run_code("1", "restricted", wall_seconds=0)


def test_hardened_command_has_every_flag():
    cmd = " ".join(sandbox.docker_command("/w", "n"))
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges",
        "--user 65534:65534",
        "--pids-limit",
        "--memory",
        "--cpus",
    ):
        assert flag in cmd


def test_probe_matrix_is_judged_by_evidence():
    r = sandbox.probe(["subprocess", "restricted"], wall_seconds=1.5)
    by = {a["id"]: a["results"] for a in r["attacks"]}
    for atk in (
        "env_secret",
        "host_file_read",
        "network_egress",
        "host_file_write",
        "spawn_process",
    ):
        assert by[atk]["subprocess"]["verdict"] == "got through", atk
        assert by[atk]["restricted"]["verdict"] == "stopped", atk
    assert by["runaway_loop"]["restricted"]["verdict"] == "contained"
    assert by["output_flood"]["restricted"]["verdict"] == "contained"
    assert r["got_through"]["restricted"] == 0 and r["got_through"]["subprocess"] == 5


# ---------------------------------------------------------------- agents


def run(core, scenario, **kw):
    r = core.runner.start("initech", scenario, wait=True, **kw)
    return r, [e["kind"] for e in core.runner.events(r["id"])]


def test_calculator_and_search_tools():
    assert calc("2 + 3 * 4") == 14
    with pytest.raises(ToolError):
        calc("__import__('os')")
    with pytest.raises(ToolError):
        calc("9 ** 999")
    assert "14 days" in search_docs("refund policy")
    assert search_docs("zebra quantum") == "no matching documents"


def test_research_completes_with_a_grounded_answer(core):
    r, kinds = run(core, "research")
    assert r["status"] == "completed" and "answer" in kinds
    ev = next(e for e in core.runner.events(r["id"]) if e["kind"] == "answer")
    assert ev["data"]["grounding"] == 1.0
    assert "14 days" in ev["data"]["text"] or "business days" in ev["data"]["text"], (
        "the answer must come from the documents"
    )


def test_loop_is_detected(core):
    r, _ = run(core, "loop")
    assert (
        r["status"] == "budget_stopped"
        and r["result"]["limit"] == "loop"
        and r["result"]["steps"] == 3
    )


def test_cost_limit(core):
    r, _ = run(core, "spendthrift")
    assert r["status"] == "budget_stopped" and r["result"]["limit"] == "cost"
    assert r["result"]["usd"] >= 0.2


def test_step_limit(core):
    r, _ = run(core, "chatty")
    assert (
        r["status"] == "budget_stopped"
        and r["result"]["limit"] == "steps"
        and r["result"]["steps"] == 6
    )


def test_time_limit(core):
    r, _ = run(core, "slow")
    assert r["status"] == "budget_stopped" and r["result"]["limit"] == "time"


def test_tool_call_limit(core):
    r, _ = run(core, "chatty", limits={"max_tool_calls": 2, "max_steps": 20, "max_repeats": 9})
    assert r["result"]["limit"] == "tool_calls"


def wait_status(core, rid, status, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if core.runner.get(rid)["status"] == status:
            return
        time.sleep(0.05)
    raise AssertionError(f"run never reached {status}: {core.runner.get(rid)['status']}")


def test_approval_gate_approve(core):
    r = core.runner.start("initech", "email")
    wait_status(core, r["id"], "awaiting_approval")
    assert core.runner.get(r["id"])["pending"]["tool"] == "send_email"
    core.runner.decide(r["id"], True)
    core.runner.threads[r["id"]].join(10)
    done = core.runner.get(r["id"])
    assert done["status"] == "completed" and "sent" in done["result"]["answer"]


def test_approval_gate_deny(core):
    r = core.runner.start("initech", "delete")
    wait_status(core, r["id"], "awaiting_approval")
    core.runner.decide(r["id"], False)
    core.runner.threads[r["id"]].join(10)
    done = core.runner.get(r["id"])
    assert done["status"] == "completed" and "declined" in done["result"]["answer"]
    assert "deleted rows" not in " ".join(str(e["data"]) for e in core.runner.events(r["id"]))


def test_approval_wait_does_not_spend_the_time_budget(core):
    r = core.runner.start("initech", "email", limits={"max_seconds": 3})
    wait_status(core, r["id"], "awaiting_approval")
    time.sleep(3.5)
    core.runner.decide(r["id"], True)
    core.runner.threads[r["id"]].join(10)
    assert core.runner.get(r["id"])["status"] == "completed"


def test_ceiling_controls_what_needs_approval(core):
    r, kinds = run(core, "email", ceiling="external")
    assert r["status"] == "completed" and "approval_required" not in kinds


def test_cancel_a_paused_run(core):
    r = core.runner.start("initech", "email")
    wait_status(core, r["id"], "awaiting_approval")
    core.runner.stop(r["id"])
    core.runner.threads[r["id"]].join(10)
    assert core.runner.get(r["id"])["status"] == "cancelled"


def test_generated_code_runs_inside_the_chosen_profile(core):
    r, _ = run(core, "code", profile="restricted")
    assert r["status"] == "completed" and "mean" in r["result"]["answer"]


def test_escape_attempt_is_blocked_when_restricted_and_gated_when_not(core):
    r, kinds = run(core, "code-escape", profile="restricted")
    assert "tool_error" in kinds and "approval_required" not in kinds
    r = core.runner.start("initech", "code-escape", profile="subprocess")
    wait_status(core, r["id"], "awaiting_approval")
    core.runner.decide(r["id"], False)
    core.runner.threads[r["id"]].join(10)
    assert any(
        e["kind"] == "approval" and e["data"]["decision"] == "denied"
        for e in core.runner.events(r["id"])
    )
    assert core.runner.get(r["id"])["status"] == "completed"


def test_agent_steps_are_gateway_calls_billed_to_the_tenant(core):
    run(core, "research")
    n = core.store.one(
        "SELECT COUNT(*) AS n, SUM(usd) AS u FROM calls WHERE tenant='initech' AND source='agent'"
    )
    assert n["n"] >= 4 and n["u"] > 0


def test_events_are_ordered_and_resumable(core):
    r, _ = run(core, "research")
    ev = core.runner.events(r["id"])
    assert [e["seq"] for e in ev] == sorted(e["seq"] for e in ev)
    assert core.runner.events(r["id"], after=ev[2]["seq"])[0]["seq"] == ev[3]["seq"]


def test_validation(core):
    for bad in (
        {"scenario": "nope"},
        {"profile": "nope"},
        {"ceiling": "root"},
        {"limits": {"bogus": 1}},
        {"limits": {"max_steps": 0}},
    ):
        with pytest.raises(ValueError):
            core.runner.start("initech", **{"scenario": "research", **bad})
    with pytest.raises(ValueError):
        core.runner.start("ghost", "research")


def test_interrupted_runs_are_marked_on_restart(core):
    core.store.run(
        "INSERT INTO runs(id, created, tenant, scenario, goal, status) VALUES('x',0,'a','b','c','running')"
    )
    from llm_control_room.agents import AgentRunner

    AgentRunner(core.store, core.gateway)
    assert core.store.one("SELECT status FROM runs WHERE id='x'")["status"] == "interrupted"


def test_a_profile_that_cannot_run_anything_is_reported_not_scored(monkeypatch):
    real = sandbox.run_code

    def fake(code, profile, **kw):
        if profile == "restricted":
            return {
                "stdout": "",
                "stderr": "boom: image missing\n",
                "exit_code": 125,
                "timed_out": False,
                "output_truncated": False,
                "elapsed_s": 0.1,
            }
        return real(code, profile, **kw)

    monkeypatch.setattr(sandbox, "run_code", fake)
    r = sandbox.probe(["subprocess", "restricted"], wall_seconds=1.5)
    assert r["profiles"] == ["subprocess"] and "image missing" in r["unusable"]["restricted"]
    assert "restricted" not in r["got_through"], (
        "an unusable profile must not look like a perfect score"
    )


needs_docker = pytest.mark.skipif(
    not sandbox.docker_available(), reason="no Docker daemon or image"
)


@needs_docker
def test_hardened_profile_in_docker_stops_the_attacks():
    r = sandbox.probe(["hardened"], wall_seconds=3)
    assert r["profiles"] == ["hardened"] and not r["unusable"]
    assert r["got_through"]["hardened"] == 0
    assert (
        sandbox.run_code("import os; print(os.getuid())", "hardened")["stdout"].strip() == "65534"
    )
