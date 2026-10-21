"""Check the demo media against the files its numbers come from. Exits 1 on any mismatch.

    uv run --with fpdf2 --with imageio-ffmpeg python scripts/check_media.py

Every number in the LinkedIn post, the blog posts and the manual text must be one that
docs/demo/run.json (the text the app returned during the recording) or docs/bench-obs-output.txt
(scripts/bench_obs.py) actually contains; narration and captions carry no numbers at all; the
subtitles are the captions in order, inside the video's length; the manual has one page per scene.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "llm-control-room"
DEMO = ROOT / "docs" / "demo"
MEDIA = Path(r"D:\github\_media") / REPO
bad: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        bad.append(what)


run = json.loads((DEMO / "run.json").read_text(encoding="utf-8"))
out = run["outputs"]

# figures re-derived here from the raw page text, not taken from run["numbers"]
src: dict[str, str] = {}
m = re.search(r"([\d.]+)% less than always", out["router"])
src["saved_pct"] = m.group(1)
src["agent_limit_usd"] = json.loads(out["agent-limits"])["max_usd"]
m = re.search(r"GOT THROUGH\s+(\d+) OF (\d+)\s+(\d+) OF (\d+)", out["sandbox"], re.I)
src["sub_through"], src["attacks"], src["res_through"] = m.group(1), m.group(2), m.group(3)
lines = (ROOT / "docs" / "bench-obs-output.txt").read_text(encoding="utf-8").splitlines()
i = next(i for i, ln in enumerate(lines) if ln.startswith("n |"))
cols = [c.strip() for c in lines[i].split("|")]
rows = [
    dict(zip(cols, (v.strip() for v in ln.split("|")), strict=True))
    for ln in lines[i + 1 :]
    if "|" in ln and ln.split("|")[0].strip().isdigit()
]
big = max(rows, key=lambda x: int(x["n"]))
src["bench_n"] = f"{int(big['n']):,}"
src["bench_rps"] = str(round(float(big["chat_rps_obs"])))
src["bench_p95"] = big["chat_p95_obs"]
for k, v in src.items():
    check(str(run["numbers"][k]) == str(v), f"run.json numbers.{k} = {v} (re-read from source)")

post = (MEDIA / "post_linkedin.txt").read_text(encoding="utf-8")
check(len(post) <= 1300, f"LinkedIn post is {len(post)} characters (<= 1,300)")
check("https://github.com/hammasbuilds/llm-control-room" in post, "post links the repo")
check("https://llm-control-room.vercel.app" in post, "post links the showcase page")
for word in ("Claude", "AI-generated", "Qwen", "GPT", "Llama"):
    check(word not in post, f"post does not say {word!r}")

allowed = set(src.values())


def nums(text: str) -> list[str]:
    text = re.sub(r"https?://\S+|#\w+|\b[\w.-]+\.(?:py|txt|jsonl|csv|md)\b", " ", text)
    return [t.rstrip(".,") for t in re.findall(r"(?<![A-Za-z\d])\$?\d[\d,.]*", text)]


for t in nums(post):
    check(t.lstrip("$") in allowed, f"post number {t} is in a source file")
for f in sorted(MEDIA.glob("blog_*.md")):
    for t in nums(f.read_text(encoding="utf-8")):
        if t.startswith("p") or t in ("50", "95", "99"):
            continue
        check(t.lstrip("$") in allowed, f"{f.name} number {t} is in a source file")

# manual text: every figure quoted from this run appears in that run's page text
alltext = "\n".join(out.values())
out_tokens = set(nums(alltext))
for sc in run["scenes"]:
    check(
        not re.search(r"\d", sc["narration"] + sc["caption"]),
        f"{sc['key']}: narration and caption carry no digits",
    )
    tail = sc["manual_full"][len(sc["manual_full"].split(" In this run")[0]) :]
    for t in nums(tail):
        check(
            t in out_tokens or t.lstrip("$") in allowed,
            f"manual {sc['key']}: {t} appears in the run's output",
        )

# subtitles = captions in order, inside the video
srt = (DEMO / f"{REPO}-demo.srt").read_text(encoding="utf-8")
caps = [b.split("\n", 2)[2].strip() for b in srt.strip().split("\n\n")]
check(caps == [sc["caption"] for sc in run["scenes"]], "subtitles are the scene captions in order")
times = re.findall(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", srt)


def sec(h, m_, s, ms):
    return int(h) * 3600 + int(m_) * 60 + int(s) + int(ms) / 1000


import imageio_ffmpeg  # noqa: E402

for name in (f"{REPO}-demo.mp4", f"{REPO}-demo-fast.mp4"):
    p = DEMO / name
    info = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(p)], capture_output=True, text=True
    ).stderr
    d = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", info)
    dur = sec(*d.groups()[:3], int(d.group(4)) * 10)
    check(p.stat().st_size < 25e6, f"{name}: {p.stat().st_size / 1e6:.1f} MB, {dur:.1f} s")
    check("1280x800" in info, f"{name}: 1280x800")
    if name.endswith("demo.mp4"):
        check("Audio:" in info, f"{name}: has a narration track")
        check(all(sec(*t[4:]) <= dur + 0.1 for t in times), "every subtitle ends inside the video")
        tl = run["timeline"]
        check(
            all(abs(sec(*t[:4]) - x["voice_start"]) < 0.01 for t, x in zip(times, tl, strict=True)),
            "each subtitle starts with its narration clip",
        )

from fpdf import FPDF  # noqa: E402,F401  (fpdf2 present)

pdf = (DEMO / f"{REPO}-manual.pdf").read_bytes()
pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
check(pages == len(run["scenes"]) + 1, f"manual has {pages} pages (cover + one per scene)")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
check(
    f"**Demo video:** [{REPO}-demo.mp4](docs/demo/{REPO}-demo.mp4) (narrated, subtitled) · "
    f"[user manual (PDF)](docs/demo/{REPO}-manual.pdf)" in readme,
    "README demo line",
)
for f in (f"{REPO}-demo.mp4", f"{REPO}-demo-fast.mp4", f"{REPO}-manual.pdf"):
    check((MEDIA / f).read_bytes() == (DEMO / f).read_bytes(), f"_media copy of {f} matches")

print(f"{len(bad)} problem(s)")
sys.exit(1 if bad else 0)
