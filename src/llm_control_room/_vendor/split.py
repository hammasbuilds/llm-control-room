# Vendored copy. Source: model-serving-platform/src/serving/routing/split.py (commit 7173605), MIT licence, (c) 2026 Muhammad Hammas. Unmodified.
"""Traffic splitting.

Assignment is a hash of the request's identity, not a coin flip. Random assignment
means the same user can land on the champion and then the challenger within one
session, which produces an inconsistent experience and an A/B result that measures
nothing. Hashing makes assignment **sticky and reproducible**: the same key always
lands in the same bucket, and the split can be replayed exactly during an
investigation.
"""

from __future__ import annotations

import hashlib


def bucket(key: str, *, salt: str = "") -> float:
    """Map a key to a stable point in [0, 1).

    The salt lets two independent experiments on the same user be uncorrelated - a
    user who is unlucky in one should not be systematically unlucky in every one.
    """
    digest = hashlib.sha256(f"{salt}\x00{key}".encode()).digest()
    # 8 bytes is ample resolution and avoids the modulo bias of a smaller slice.
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def in_canary(key: str, traffic: float, *, salt: str = "") -> bool:
    if traffic <= 0.0:
        return False
    if traffic >= 1.0:
        return True
    return bucket(key, salt=salt) < traffic
