import pytest

from llm_control_room.providers import MOCK_MODELS
from llm_control_room.router import decide, level_for, measure

M = list(MOCK_MODELS)


def test_levels():
    assert decide("Hi there", M).level == 0
    assert decide("What is a webhook?", M).level == 0
    assert decide("How do I configure retries for an API client?", M).level == 1
    assert decide("Analyse the trade-offs between a monolith and microservices", M).level == 2
    assert decide("Refactor this:\n```\ndef f(): pass\n```", M).level == 2


def test_length_is_a_signal():
    short, long = measure("Tell me about it"), measure("word " * 400)
    assert long.score > short.score and level_for(long.score) == 2


def test_cheapest_model_that_is_enough():
    d = decide("Hi there", M, min_quality=0.9)
    assert d.primary == "nano-mock" and d.chain[0] == "swift-mock"
    d = decide("How should we structure feedback for a junior engineer?", M, min_quality=0.8)
    assert d.primary == "swift-mock"
    d = decide("Prove that sqrt(2) is irrational, step by step.", M, min_quality=0.9)
    assert d.primary == "titan-mock"


def test_higher_threshold_never_picks_cheaper():
    prompt = "Explain how an index speeds up queries"
    cost = [decide(prompt, M, min_quality=q).expected_usd for q in (0.5, 0.8, 0.9, 0.95)]
    assert cost == sorted(cost)


def test_nothing_enough_uses_strongest_allowed():
    d = decide(
        "Prove this theorem step by step", M, min_quality=0.99, allowed=["nano-mock", "swift-mock"]
    )
    assert d.primary == "swift-mock" and "strongest permitted" in d.reason


def test_allowed_restricts_and_empty_pool_fails():
    assert decide("hello", M, allowed=["sage-mock"]).primary == "sage-mock"
    with pytest.raises(ValueError):
        decide("hello", M, allowed=["not-a-model"])


def test_pinned_and_unknown_pin():
    d = decide("hello", M, pinned="titan-mock")
    assert d.primary == "titan-mock" and d.reason == "pinned by caller" and d.chain
    with pytest.raises(ValueError):
        decide("hello", M, pinned="nope")


def test_baseline_and_saving():
    d = decide("Hi there", M)
    assert d.baseline_model == "titan-mock" and d.baseline_usd > d.expected_usd
    assert decide("Hi", M, baseline="sage-mock").baseline_model == "sage-mock"


def test_context_raises_difficulty():
    ctx = "Refunds take days. " * 30
    assert (
        measure("How long do refunds take?", ctx).score > measure("How long do refunds take?").score
    )
