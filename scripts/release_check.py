"""How often does auto-rollback decide correctly? Replay each canary scenario over many seeds.

    uv run python scripts/release_check.py [seeds]

Expected: canary-good is never rolled back, canary-bad always is, canary-outage is always held
(never rolled back, because the cause is the provider). Every figure printed is counted here.
"""

from __future__ import annotations

import sys
from collections import Counter

from llm_control_room.app import create_app

SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
EXPECT = {"canary-good": "kept", "canary-bad": "rolled back", "canary-outage": "held"}


def outcome(core, release: str) -> str:
    kinds = [e["kind"] for e in core.releases.events(release, 100)]
    if "auto_rollback" in kinds:
        return "rolled back"
    if "rollback_held" in kinds:
        return "held"
    return "kept"


def main() -> None:
    app = create_app(":memory:")
    sim, core = app.state.sim, app.state.core
    print(f"{SEEDS} seeds per scenario, 600 requests each over a simulated 24 hours\n")
    print(f"{'scenario':<15}{'kept':>6}{'held':>6}{'rolled back':>13}   expected")
    bad = 0
    for scenario, want in EXPECT.items():
        tally: Counter = Counter()
        for seed in range(1, SEEDS + 1):
            sim.run(scenario, n=600, seed=seed)
            tally[outcome(core, "support-bot")] += 1
        right = tally[want]
        bad += SEEDS - right
        print(
            f"{scenario:<15}{tally['kept']:>6}{tally['held']:>6}{tally['rolled back']:>13}   "
            f"{want} ({right}/{SEEDS} as expected)"
        )
    sys.exit(1 if bad else 0)


main()
