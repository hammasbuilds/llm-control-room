"""Run scripts/bench_obs.py three times and write all runs plus their median.

    uv run python scripts/bench_obs_median.py            # writes docs/bench-obs-output.txt

Each run is a fresh process (same arguments: N = 30,000 and 300,000). Before and after each run
the whole machine's CPU load is sampled, because the numbers depend on what else is running.
The last table in the file is the per-column median of the three runs; the README's Performance
line and the demo posts quote that table (scripts/check_media.py checks it).
"""

from __future__ import annotations

import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench-obs-output.txt"
RUNS = 3


def cpu_load() -> str:
    """Whole-machine CPU load in percent (Windows), or 'n/a'."""
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.stdout.strip() + "%" if r.stdout.strip() else "n/a"
    except Exception:
        return "n/a"


def table(text: str) -> tuple[list[str], list[dict]]:
    lines = text.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("n |"))
    cols = [c.strip() for c in lines[i].split("|")]
    rows = [
        dict(zip(cols, (v.strip() for v in ln.split("|")), strict=True))
        for ln in lines[i + 1 :]
        if "|" in ln and ln.split("|")[0].strip().isdigit()
    ]
    return cols, rows


def main() -> int:
    started = datetime.now().isoformat(timespec="minutes")
    parts, tables = [], []
    for k in range(1, RUNS + 1):
        before = cpu_load()
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bench_obs.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        after = cpu_load()
        if r.returncode:
            print(r.stderr)
            return 1
        cols, rows = table(r.stdout)
        tables.append(rows)
        parts.append(
            f"## run {k} of {RUNS} (machine CPU load before {before}, after {after})\n"
            + r.stdout.strip()
            + "\n"
        )
        print(parts[-1], flush=True)
    med = []
    for i, row in enumerate(tables[0]):
        m = {"n": row["n"]}
        for c in cols[1:]:
            vals = [float(t[i][c]) for t in tables]
            v = statistics.median(vals)
            m[c] = str(int(v)) if all(x.is_integer() for x in vals) else f"{v:.1f}"
        med.append(m)
    head = (
        f"# scripts/bench_obs.py, {RUNS} runs, started {started}\n"
        "# Measured on a shared desktop (Windows 11, CPU only) while other work was running on it,\n"
        "# so absolute figures move between runs; the CPU load sampled around each run is noted.\n"
        "# Times in ms; chat_rps_obs = chat requests a second while /api/obs is requested back to\n"
        "# back. The last table is the per-column median of the runs and is what the README quotes.\n"
    )
    med_txt = (
        f"## median of {RUNS} runs\n"
        + " | ".join(cols)
        + "\n"
        + "\n".join(" | ".join(m[c] for c in cols) for m in med)
        + "\n"
    )
    OUT.write_text(head + "\n" + "\n".join(parts) + "\n" + med_txt, encoding="utf-8")
    print(med_txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
