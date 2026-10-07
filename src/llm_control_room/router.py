"""Send each request to the cheapest model that is enough.

Difficulty is estimated from the request text with cheap, explainable signals (no model call,
because a classifier call on every request is the cost the router exists to avoid). Length is
the first signal because router-14b measured how strongly length tracks what people prefer
(see the README); the others are the usual cues for reasoning, code and multi-part asks.

A model is "enough" when its quality for that difficulty level reaches the tenant's
``min_quality``. Among the models that are enough the router takes the one with the lowest
expected cost; the rest of the chain is the other sufficient models by price, then the
strongest remaining ones, so a provider failure degrades to a better model rather than a worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .providers import ModelInfo, count_tokens

LEVELS = ("easy", "medium", "hard")

# Expected completion length by difficulty; only used to price a decision before the call.
EXPECTED_COMPLETION_TOKENS = (45, 140, 380)

_HARD_WORDS = re.compile(
    r"\b(prove|derive|analy[sz]e|critique|refactor|optimi[sz]e|trade-?offs?|architecture|"
    r"step[- ]by[- ]step|reason through|root cause|why does|why is my|race condition|"
    r"complexity|invariant|design a|implement)\b",
    re.I,
)
_CODE = re.compile(
    r"```|\bdef \w+\(|\bclass \w+|\bSELECT\b.+\bFROM\b|=>|\bfunction\b|\bstack ?trace\b",
    re.I | re.S,
)
_EASY_CUES = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|good morning)\b|"
    r"\b(translate|spell[- ]?check|rephrase|classify|sentiment|label this|what is an?|define)\b",
    re.I,
)
_TASK = re.compile(
    r"\b(draft|write|explain|compare|recommend|plan|outline|describe|summari[sz]e|"
    r"how (?:do|should|can|would|to)|what should)\b",
    re.I,
)
_MATH = re.compile(
    r"\d\s*[-+*/^=]\s*\d|\b(integral|derivative|matrix|probability|equation)\b", re.I
)
_MULTI = re.compile(r"(^|\n)\s*(\d+[.)]|[-*]) ", re.M)


@dataclass
class Signals:
    words: int
    chars: int
    points: dict[str, float] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return round(sum(self.points.values()), 2)


def measure(text: str, context: str = "") -> Signals:
    words = len(text.split())
    s = Signals(words=words, chars=len(text))
    # Length: a graded contribution, so the first 25 words add nothing and 300 add a lot.
    s.points["length"] = round(min(3.0, max(0.0, (words - 25) / 90)), 2)
    if context:
        s.points["context"] = round(0.7 + min(0.6, len(context.split()) / 400), 2)
    if _CODE.search(text):
        s.points["code"] = 1.6
    if _HARD_WORDS.search(text):
        s.points["reasoning_words"] = 2.0
    elif _TASK.search(text):
        s.points["task_verb"] = 0.8
    if _MATH.search(text):
        s.points["math"] = 0.8
    parts = len(_MULTI.findall(text)) + max(0, text.count("?") - 1)
    if parts >= 2:
        s.points["multi_part"] = round(min(1.2, 0.4 * parts), 2)
    if _EASY_CUES.search(text) and words < 60:
        s.points["easy_cue"] = -1.2
    return s


# score < MEDIUM_AT -> easy; score >= HARD_AT -> hard
MEDIUM_AT = 0.5
HARD_AT = 2.0


def level_for(score: float) -> int:
    return 2 if score >= HARD_AT else 1 if score >= MEDIUM_AT else 0


@dataclass
class Decision:
    level: int
    signals: Signals
    primary: str
    chain: list[str]
    reason: str
    expected_usd: float
    baseline_model: str
    baseline_usd: float
    candidates: list[dict]
    min_quality: float

    def to_dict(self) -> dict:
        return {
            "difficulty": LEVELS[self.level],
            "level": self.level,
            "score": self.signals.score,
            "signals": {
                "words": self.signals.words,
                "chars": self.signals.chars,
                "points": self.signals.points,
            },
            "primary": self.primary,
            "chain": self.chain,
            "reason": self.reason,
            "expected_usd": self.expected_usd,
            "baseline_model": self.baseline_model,
            "baseline_usd": self.baseline_usd,
            "expected_saving_usd": round(self.baseline_usd - self.expected_usd, 8),
            "min_quality": self.min_quality,
            "candidates": self.candidates,
        }


def baseline_for(models: list[ModelInfo], configured: str = "") -> ModelInfo:
    """What the same request would have cost on the 'just use the best model' plan."""
    if configured:
        hit = next((m for m in models if m.id == configured), None)
        if hit:
            return hit
    return max(models, key=lambda m: (m.usd_out, m.usd_in))


def decide(
    text: str,
    models: list[ModelInfo],
    *,
    context: str = "",
    min_quality: float = 0.75,
    allowed: list[str] | None = None,
    pinned: str | None = None,
    extra_fallbacks: list[str] | None = None,
    baseline: str = "",
) -> Decision:
    sig = measure(text, context)
    level = level_for(sig.score)
    pool = [m for m in models if not allowed or m.id in allowed]
    if not pool:
        raise ValueError("no permitted model for this tenant")
    ptoks = count_tokens(text) + count_tokens(context)
    ctoks = EXPECTED_COMPLETION_TOKENS[level]
    base = baseline_for(models, baseline)
    base_usd = base.cost(ptoks, ctoks)

    rows = []
    for m in pool:
        rows.append(
            {
                "model": m.id,
                "quality": m.quality[level],
                "usd": round(m.cost(ptoks, ctoks), 8),
                "enough": m.quality[level] >= min_quality,
            }
        )
    enough = sorted((r for r in rows if r["enough"]), key=lambda r: (r["usd"], -r["quality"]))
    rest = sorted((r for r in rows if not r["enough"]), key=lambda r: (-r["quality"], r["usd"]))

    if pinned:
        info = next((m for m in models if m.id == pinned), None)
        if info is None:
            raise ValueError(f"unknown model {pinned!r}")
        order = [pinned] + [r["model"] for r in enough + rest if r["model"] != pinned]
        reason = "pinned by caller"
        expected = info.cost(ptoks, ctoks)
    else:
        if enough:
            order = [r["model"] for r in enough] + [r["model"] for r in rest]
            reason = (
                f"{LEVELS[level]} request: cheapest model with quality >= {min_quality:g} "
                f"for {LEVELS[level]} work"
            )
        else:
            order = [r["model"] for r in rest]
            reason = (
                f"no model reaches quality {min_quality:g} for {LEVELS[level]} work; "
                "using the strongest permitted"
            )
        expected = next(r["usd"] for r in rows if r["model"] == order[0])
    chain = order + [
        m for m in (extra_fallbacks or []) if m not in order and m in {x.id for x in models}
    ]
    return Decision(
        level,
        sig,
        chain[0],
        chain[1:4],
        reason,
        round(expected, 8),
        base.id,
        round(base_usd, 8),
        rows,
        min_quality,
    )
