# Vendored copy. Source: bounded-agent-runtime/src/bar/budget/limits.py (commit ea33a50), MIT licence, (c) 2026 Muhammad Hammas. Unmodified.
"""Budgets that the runtime enforces, not the agent.

The premise of the project: an agent cannot be trusted to respect its own limits,
because the thing being limited is the thing doing the checking. So every ceiling
lives outside the agent loop, is checked before each step rather than after, and
raises rather than returns - a returned error can be ignored by a caller, an
exception unwinds the loop whether it wants to or not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised the moment a ceiling is crossed. Carries which one, for the audit log."""

    def __init__(self, kind: str, limit: float, used: float) -> None:
        self.kind, self.limit, self.used = kind, limit, used
        super().__init__(f"{kind} budget exhausted: used {used:.4g} of {limit:.4g}")


@dataclass
class Budget:
    """A ceiling on one run. Every field is a hard stop, not a warning."""

    max_steps: int = 20
    max_seconds: float = 120.0
    max_usd: float = 0.25
    max_tool_calls: int = 40
    # A run that keeps producing the same action is looping, not working.
    max_repeats: int = 3

    steps: int = 0
    tool_calls: int = 0
    usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    _fingerprints: list[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        """Called before every step. Ordered cheapest-first."""
        if self.steps >= self.max_steps:
            raise BudgetExceeded("steps", self.max_steps, self.steps)
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("tool_calls", self.max_tool_calls, self.tool_calls)
        if self.usd >= self.max_usd:
            raise BudgetExceeded("cost", self.max_usd, self.usd)
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded("time", self.max_seconds, self.elapsed)

    def record_step(self, fingerprint: str) -> None:
        """Count a step and detect repetition.

        Loop detection is by exact repetition of the action fingerprint rather than
        by similarity: it is cheap, has no false positives worth worrying about, and
        catches the failure that actually happens - an agent re-issuing the identical
        call because the observation did not change.
        """
        self.steps += 1
        self._fingerprints.append(fingerprint)
        recent = self._fingerprints[-self.max_repeats :]
        if len(recent) == self.max_repeats and len(set(recent)) == 1:
            raise BudgetExceeded("loop", self.max_repeats, self.max_repeats)

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_cost(self, usd: float) -> None:
        self.usd += usd

    def remaining(self) -> dict[str, float]:
        return {
            "steps": self.max_steps - self.steps,
            "tool_calls": self.max_tool_calls - self.tool_calls,
            "usd": round(self.max_usd - self.usd, 6),
            "seconds": round(self.max_seconds - self.elapsed, 2),
        }
