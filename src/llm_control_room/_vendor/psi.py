# Vendored copy. Source: llm-observability-platform/src/llmobs/drift/psi.py (commit 790d19d), MIT licence, (c) 2026 Muhammad Hammas. Unmodified.
"""Drift detection by Population Stability Index.

PSI is the standard measure in risk modelling for "has the input distribution moved",
and it transfers cleanly to LLM inputs. Implemented here in pure Python - it is a
handful of logarithms, and pulling in numpy for that would be the tail wagging the dog.

    PSI = Σ (actual% − expected%) × ln(actual% / expected%)

Conventional reading, inherited from credit scoring and worth keeping because people
already know it:

    < 0.10   no meaningful shift
    0.10-0.25 moderate shift - worth looking at
    > 0.25   significant shift - investigate

**Why this matters for an LLM application.** Prompt drift is the failure nobody
instruments. A system is built and tuned against one kind of input; three months later
users are asking something different, quality quietly falls, and no error is ever
raised because nothing errored. Watching the distribution of prompt *shape* catches
that without storing a single prompt.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence

# Guards against a zero bucket sending the logarithm to infinity. Small enough not to
# distort a real signal, large enough to keep the arithmetic finite.
_EPSILON = 1e-6


def bucketise(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    """Proportion of values falling in each bucket defined by `edges`."""
    if not values:
        return [0.0] * (len(edges) + 1)
    counts = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for i, edge in enumerate(edges):
            if v < edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    total = len(values)
    return [c / total for c in counts]


def psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """PSI between two distributions already expressed as proportions."""
    if len(expected) != len(actual):
        raise ValueError("distributions must have the same number of buckets")
    total = 0.0
    for e, a in zip(expected, actual, strict=False):
        e = max(e, _EPSILON)
        a = max(a, _EPSILON)
        total += (a - e) * math.log(a / e)
    return round(total, 6)


def psi_numeric(
    reference: Sequence[float], current: Sequence[float], *, buckets: int = 10
) -> float:
    """PSI over two samples of a numeric feature - prompt length, latency, cost.

    Bucket edges come from the **reference** quantiles, not from the combined data.
    Deriving them from both would let the current window move the goalposts and hide
    the very shift being measured.
    """
    if not reference or not current:
        return 0.0
    ordered = sorted(reference)
    edges = [
        ordered[min(len(ordered) - 1, int(len(ordered) * i / buckets))] for i in range(1, buckets)
    ]
    # Ties collapse buckets; a constant reference has no distribution to shift.
    edges = sorted(set(edges))
    if not edges:
        return 0.0
    return psi(bucketise(reference, edges), bucketise(current, edges))


def psi_categorical(reference: Iterable[str], current: Iterable[str]) -> float:
    """PSI over categorical values - model used, feature name, prompt signature."""
    ref = Counter(reference)
    cur = Counter(current)
    if not ref or not cur:
        return 0.0
    # Union of categories, so one appearing only in the current window still counts.
    keys = sorted(set(ref) | set(cur))
    ref_total, cur_total = sum(ref.values()), sum(cur.values())
    return psi(
        [ref[k] / ref_total for k in keys],
        [cur[k] / cur_total for k in keys],
    )


def interpret(value: float) -> str:
    if value < 0.10:
        return "stable"
    if value < 0.25:
        return "moderate shift"
    return "significant shift"
