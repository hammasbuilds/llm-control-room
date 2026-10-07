import pytest
from conftest import ask

from llm_control_room.releases import Obs, ReleaseError, two_proportion_z


def setup_release(core, v2_model="auto", v2_prompt="", traffic=0.3, mode="canary", **slo):
    rel = core.releases
    rel.create("bot", model="auto", system_prompt="be brief", slo={"min_samples": 30, **slo})
    rel.add_version("bot", model=v2_model, system_prompt=v2_prompt, note="v2")
    rel.start_canary("bot", 2, traffic, mode)
    return rel


def drive(core, n, model="bot", prefix="Explain how an index works", difficulty=1):
    for i in range(n):
        try:
            ask(
                core,
                f"{prefix} ({i})",
                model=model,
                session_key=f"user{i}",
                use_cache=False,
                true_difficulty=difficulty,
            )
        except Exception:
            pass


def test_create_and_lifecycle(core):
    rel = core.releases
    rel.create("bot")
    assert rel.get("bot")["champion"] == 1
    with pytest.raises(ReleaseError):
        rel.create("bot")
    rel.add_version("bot", model="swift-mock")
    with pytest.raises(ReleaseError):
        rel.start_canary("bot", 1, 0.1)  # the champion
    with pytest.raises(ReleaseError):
        rel.start_canary("bot", 2, 1.5)
    rel.start_canary("bot", 2, 0.2)
    rel.add_version("bot")
    with pytest.raises(ReleaseError):
        rel.start_canary("bot", 3, 0.2)  # one canary at a time
    rel.promote("bot", 2)
    r = rel.get("bot")
    assert r["champion"] == 2 and r["challenger"] is None
    assert [v["stage"] for v in r["versions"]] == ["archived", "champion", "archived"]
    with pytest.raises(ReleaseError):
        rel.rollback("bot")


def test_assignment_is_sticky_and_proportional(core):
    rel = setup_release(core, traffic=0.3)
    first = {k: rel.resolve("bot", f"t:{k}")[0]["version"] for k in range(1000)}
    again = {k: rel.resolve("bot", f"t:{k}")[0]["version"] for k in range(1000)}
    assert first == again
    share = sum(1 for v in first.values() if v == 2) / 1000
    assert 0.25 < share < 0.35


def test_a_release_is_called_like_a_model(core):
    setup_release(core)
    r = ask(core, "Hi there", model="bot", session_key="u1")
    assert r["release"] == "bot" and r["version"] in (1, 2)


def test_bad_canary_rolls_back_on_quality(core):
    setup_release(core, v2_prompt="[[mock quality=-0.8]]", traffic=0.4)
    drive(core, 400)
    r = core.releases.get("bot")
    assert r["challenger"] is None and r["champion"] == 1
    kinds = [e["kind"] for e in core.releases.events("bot")]
    assert "auto_rollback" in kinds
    assert core.releases.last_check["bot"]["verdict"] == "rolled_back"


def test_good_canary_is_left_alone(core):
    setup_release(core, v2_prompt="be warm", traffic=0.4)
    drive(core, 300)
    r = core.releases.get("bot")
    assert r["challenger"] == 2
    assert core.releases.last_check["bot"]["verdict"] in ("ok", "insufficient_samples")


def test_outage_hitting_both_versions_holds_instead_of_rolling_back(core):
    setup_release(core, v2_model="auto", traffic=0.4)
    core.providers.mock.set_fault("swift-mock", error_rate=0.7)
    drive(core, 400)
    r = core.releases.get("bot")
    assert r["challenger"] == 2, "a shared outage must not remove a healthy canary"
    assert core.releases.last_check["bot"]["verdict"] == "upstream_outage"
    assert "rollback_held" in [e["kind"] for e in core.releases.events("bot")]


def test_outage_on_the_canarys_model_seen_in_other_traffic_holds(core):
    rel = core.releases
    rel.create("bot", model="titan-mock", slo={"min_samples": 30})
    rel.add_version("bot", model="swift-mock")
    rel.start_canary("bot", 2, 0.4)
    core.providers.mock.set_fault("swift-mock", error_rate=0.7)
    for i in range(300):
        for _ in range(2):  # background traffic that is not the canary, on the same model
            try:
                ask(
                    core,
                    f"Explain how an index works ({i})",
                    tenant="initech",
                    model="swift-mock",
                    use_cache=False,
                )
            except Exception:
                pass
        try:
            ask(
                core,
                f"Explain how an index works ({i})",
                model="bot",
                session_key=f"u{i}",
                use_cache=False,
                true_difficulty=1,
            )
        except Exception:
            pass
    assert core.releases.get("bot")["challenger"] == 2
    assert core.releases.last_check["bot"]["verdict"] == "upstream_outage"
    assert "failing for other traffic" in core.releases.last_check["bot"]["breaches"][0]["why"]


def background(core, n, model="swift-mock"):
    for i in range(n):
        ask(
            core,
            f"Explain how an index works ({i})",
            tenant="initech",
            model=model,
            use_cache=False,
        )


def test_same_errors_with_a_healthy_model_elsewhere_do_roll_back(core):
    rel = core.releases
    rel.create("bot", model="titan-mock", slo={"min_samples": 30})
    # v2 is a version whose own prompt makes its calls fail; the same model is fine for others
    rel.add_version("bot", model="swift-mock", system_prompt="[[mock error=0.8]]")
    rel.start_canary("bot", 2, 0.4)
    for i in range(0, 300, 30):
        background(core, 3)
        drive(core, 30, prefix=f"Explain how an index works r{i}")
    assert core.releases.get("bot")["challenger"] is None
    why = [e for e in core.releases.events("bot") if e["kind"] == "auto_rollback"][0]["detail"][
        "reason"
    ]
    assert "healthy for other traffic" in why


def test_errors_with_no_other_traffic_on_that_model_are_held_not_guessed(core):
    rel = core.releases
    rel.create("bot", model="sage-mock", slo={"min_samples": 30})
    rel.add_version("bot", model="swift-mock", system_prompt="[[mock error=0.8]]")
    rel.start_canary("bot", 2, 0.4)
    drive(
        core, 300, difficulty=0
    )  # easy work: swift and sage are equally good, so only errors breach
    assert core.releases.get("bot")["challenger"] == 2
    assert core.releases.last_check["bot"]["verdict"] == "upstream_outage"
    assert "cannot be ruled out" in core.releases.last_check["bot"]["breaches"][0]["why"]


def test_quality_trailing_the_champion_is_caught_even_above_the_floor(core):
    rel = core.releases
    rel.create("bot", model="titan-mock", slo={"min_samples": 30})
    rel.add_version("bot", model="titan-mock", system_prompt="[[mock quality=-0.3]]")
    rel.start_canary("bot", 2, 0.4)
    drive(core, 400)
    assert core.releases.get("bot")["challenger"] is None


def test_ordinary_quality_noise_does_not_roll_back(core):
    for seed in range(5):
        core.providers.mock.reseed(seed)
        core.releases.create(f"bot{seed}", model="swift-mock", slo={"min_samples": 30})
        core.releases.add_version(f"bot{seed}", model="swift-mock", system_prompt="different words")
        core.releases.start_canary(f"bot{seed}", 2, 0.4)
        drive(core, 200, model=f"bot{seed}")
        assert core.releases.get(f"bot{seed}")["challenger"] == 2, (
            f"false rollback with seed {seed}"
        )


def test_no_verdict_below_min_samples(core):
    rel = setup_release(core, v2_prompt="[[mock quality=-0.9]]", traffic=0.5, min_samples=100)
    drive(core, 40)
    assert rel.get("bot")["challenger"] == 2
    assert rel.check("bot")["verdict"] == "insufficient_samples"


def test_auto_rollback_can_be_switched_off(core):
    rel = setup_release(core, v2_prompt="[[mock quality=-0.9]]", traffic=0.5)
    rel.set_slo("bot", {}, auto_rollback=False)
    drive(core, 200)
    assert rel.get("bot")["challenger"] == 2 and rel.last_check["bot"]["verdict"] == "bad_canary"


def test_shadow_is_recorded_but_never_served_and_cannot_break_serving(core):
    rel = core.releases
    rel.create("bot", model="swift-mock")
    rel.add_version("bot", model="titan-mock")
    rel.add_shadow("bot", 2)
    with pytest.raises(ReleaseError):
        rel.add_shadow("bot", 1)  # the champion
    r = ask(core, "Explain how an index works", model="bot", session_key="a")
    assert r["model"] == "swift-mock" and r["version"] == 1
    rows = core.store.all("SELECT shadow, model, shadow_sim FROM calls ORDER BY id")
    assert [x["shadow"] for x in rows] == [0, 1] and rows[1]["model"] == "titan-mock"
    assert 0 <= rows[1]["shadow_sim"] <= 1
    core.providers.mock.set_fault("titan-mock", error_rate=1.0)
    assert (
        ask(core, "Explain how an index works again", model="bot", session_key="b")["version"] == 1
    )
    # shadow spend is not charged to the tenant budget
    assert core.store.one("SELECT COUNT(*) AS n FROM calls WHERE shadow=1 AND error!=''")["n"] == 1


def test_ab_analysis_detects_a_real_difference(core):
    rel = core.releases
    rel.create("ab", model="nano-mock", auto_rollback=False)
    rel.add_version("ab", model="titan-mock")
    rel.start_canary("ab", 2, 0.5, "ab")
    for i in range(300):
        ask(
            core,
            f"Explain how an index works ({i})",
            model="ab",
            session_key=f"u{i}",
            use_cache=False,
            true_difficulty=1,
        )
    c = rel.analysis("ab")["comparison"]
    assert (
        c["verdict"] == "challenger better" and c["p_value"] < 0.05 and c["cost_delta_per_call"] > 0
    )


def test_two_proportion_z():
    z, p = two_proportion_z(0.5, 100, 0.5, 100)
    assert z == 0 and p == pytest.approx(1.0)
    _, p = two_proportion_z(0.5, 500, 0.6, 500)
    assert p < 0.01


def test_windows_and_health_helpers(core):
    rel = core.releases
    rel.create("bot")
    for i in range(10):
        rel.observe("bot", 1, Obs(100 + i, False, False, True, False, "m"))
    s = rel.window_stats("bot", 1)
    assert s["n"] == 10 and s["p95_ms"] > 0 and s["quality_rate"] == 1.0
    rel.health.record("m", False, "x")
    rel.health.record("m", True, "y")
    assert rel.health.error_rate("m") == (0.5, 2)
    assert rel.health.error_rate("m", exclude_tag="x") == (0.0, 1)


def test_delete_release_removes_its_calls(core):
    setup_release(core)
    ask(core, "Hi there", model="bot", session_key="u")
    core.releases.delete("bot")
    assert not core.releases.exists("bot")
    assert core.store.one("SELECT COUNT(*) AS n FROM calls WHERE release='bot'")["n"] == 0
