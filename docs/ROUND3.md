# Round 3: fresh-eyes product review, 2026-10-21

A first-time user's walk: a developer who runs two LLM apps and wants one place to gate, route and
price their calls. Order: first launch (and opening the bare address) -> Overview / Try it ->
Playground -> Tenants and keys (create a tenant, get a key, call the gateway with the curl command
the page shows) -> Router -> Observability -> Releases -> Agent runs -> Sandbox -> Simulator ->
About. Each page in headless Microsoft Edge at 1440x900 dark, 1440x900 light and 390x844 (light),
every screenshot looked at. Contrast is measured from pixels: each text element is captured twice,
as shown and with its text made transparent, and the ratio is taken between the pixels that differ
(the glyphs) and the same pixels without text, at the 90th percentile of the glyph pixels.

Axes, 0-10: **Works** (it does what the button says), **Output** (what it shows is right),
**Clear** (a new user knows what to do and what the words mean, empty states and errors say what
to do next), **Finished** (alignment, spacing, contrast, overflow, loading states).

## Before

| Page | Works | Output | Clear | Finished | Why (lowest axis) |
|---|---|---|---|---|---|
| First launch / sign-in | 4 | 6 | 2 | 3 | Bare address: a browser `prompt()` "needs an admin token (LCR_ADMIN_TOKEN)" with no word on where it is; cancel left "Something went wrong: admin token required (X-Admin-Token)". Pasting the launcher's `/#token=` address into the open tab was ignored (read only at load). |
| Overview / Try it | 9 | 9 | 6 | 6 | 8 tiles in 7 columns left one alone on a row; chart labels printed at about 5 px (640-unit drawing squeezed into a 360 px card); p95, grounding and "$/ok" unexplained; empty Releases card named the Simulator without a link. |
| Playground | 9 | 8 | 6 | 7 | curl showed `Bearer lcr-...` for any tenant without a demo key; a "length +0" bar for a signal that added nothing; "Quality" column unexplained; Feature tag field sat 12 px off the Tenant and Model fields. |
| Tenants and keys | 7 | 5 | 4 | 5 | Budget bar drawn full at $0.02 of $25 (it was scaled against itself); New tenant form at the very bottom, under every tenant; the new key appeared alone, no command to use it, and the new tenant's card did not appear until a reload; New key used a `prompt()`; labels cut off inside fields ("MIN QUALITY (ROUTE..."). |
| Router | 9 | 9 | 7 | 5 | The "two-column" grid made three columns at 1440 px: an empty third beside the first two cards, the Baseline card alone and stretched; scatter labels at 18 px. |
| Observability | 9 | 7 | 5 | 6 | Error-rate axis read 0% / 0% / 0% / 0% with no errors; spend axis label clipped (".005845"); PSI, p50/p95/p99 and "mock quality draw" unexplained; lone tile; Fault injection inputs misaligned. |
| Releases | 9 | 8 | 4 | 6 | Canary, A/B, shadow, SLO, champion/challenger, two-proportion test, "arm", "Window n" never explained; p value printed as "p 0"; release links and the create form in one mixed row; empty column beside the two lower cards. |
| Agent runs | 9 | 9 | 7 | 6 | "Approval needed above: writes" hard to read; empty third column; run-log lines wrapped under their label. |
| Sandbox | 9 | 9 | 7 | 8 | "Hardening profile" not explained in plain words. |
| Simulator | 9 | 9 | 6 | 7 | Scenario cards did not look clickable (and were `div`s, not reachable by keyboard); "Pick a scenario.", "deterministic", "backfills"; "REQUESTS PER SECO..." label cut off. |
| About and guide | 9 | 6 | 7 | 8 | Said "a budget can be overshot by one request", false since round 2 made the budget a hard cap; no glossary; no "connect your app" step. |

Contrast before (pixels, 70 selector/theme pairs): 2 under 4.5:1, both the light-theme green
status text ("stable", "ok" chips) at 4.26:1.

## After

Re-scored from new screenshots of the same walk in all three layouts (not assumed from the diff).
The curl command the Tenants page shows for a new key was run as shown in each layout: HTTP 200,
`x-lcr-model: nano-mock`.

| Page | Works | Output | Clear | Finished | What changed |
|---|---|---|---|---|---|
| First launch / sign-in | 9 | 9 | 9 | 9 | In-page sign-in box: why there is a token, the file path on Windows and macOS/Linux, `LCR_HOME`/`--db`/`LCR_ADMIN_TOKEN`, "That token was not accepted" on a wrong one; no browser dialog anywhere. A `/#token=` address pasted into an open tab signs it in. |
| Overview / Try it | 9 | 9 | 9 | 9 | Tiles fill whole rows (4 + 4); charts drawn at their real width with 11 px labels; p95 and grounding explained in their tiles; empty Releases card links to the Simulator. |
| Playground | 9 | 9 | 9 | 9 | Curl for a tenant without a demo key says which key to paste and that keys are shown once, plus the PowerShell `curl.exe` note; zero-point signals hidden and the remaining ones labelled; Quality explained; fields aligned. |
| Tenants and keys | 9 | 9 | 9 | 9 | New tenant first and full width; the key comes with Copy key, the exact curl command, Copy command and what to do if it is lost; the new card appears at once; New key shows the same panel in the tenant's card (no `prompt()`); budget meter is spend over budget with the share; labels above fields, never cut. |
| Router | 9 | 9 | 9 | 9 | Two real columns, catalogue beside the baseline; scatter at its true width; tiles say what Saved and Pinned mean. |
| Observability | 9 | 9 | 9 | 9 | Rate chart has a 10% floor and says "No fallbacks or errors in this window"; compact money axis; PSI and p50/p95/p99 in plain words; Alerts and Fault injection stacked; tenant and model cost tables stacked beside the feature table. |
| Releases | 9 | 9 | 9 | 9 | A "What a release is" panel (open when there are none) defines release, champion/challenger, canary, A/B, shadow, SLO/auto-rollback; "Your releases" and "New release" are separate cards; "p < 0.001" with signed deltas and the two-proportion test explained; plain column names. |
| Agent runs | 9 | 9 | 9 | 9 | "Ask a human before" with plain options; risk levels and limits explained; approval box says nothing happens until you choose; log in two aligned columns; scenarios are buttons. |
| Sandbox | 9 | 9 | 9 | 9 | Hardening profile defined in the page header. |
| Simulator | 9 | 9 | 9 | 9 | Scenario cards are keyboard-reachable buttons; "Click a scenario to run it"; plain subtitle. |
| About and guide | 9 | 9 | 9 | 9 | False budget sentence replaced with what the code does; a "Connect your app" step; a "Words used here" glossary (tenant, p50/p95/p99, grounding, PSI, canary, A/B, two-proportion test, shadow, SLO, hardening profile, fallback chain, redact/block). |

Contrast after (pixels, 72 selector/theme pairs): 0 under 4.5:1; the light green status text
now measures 5.83:1 (token pair 6.21:1). Lowest: light accent text on its tinted chip, 4.88:1.

Checks after: pytest 359 passed, 1 skipped (12 new tests in `tests/test_round3_review.py`, all 12
fail on the previous commit); ruff clean; `scripts/round_check.py` 163 checks, 0 wrong;
`scripts/round2_check.py` 393 cases, 0 wrong; `scripts/ui_audit.py` 1,329 controls found, 1,326
exercised, 0 rows with problems. The audit's first run after the fixes caught two new overflows
(term fields 4 px wider than their box once labels sat above them, and the budget figure on a
phone), both fixed before the second run.

## Still short of 10

- Tenant cards are long (eleven settings each); with many tenants the page is a long scroll.
  A per-tenant collapse would help; not needed for 9.
- The Router's Baseline card stretches to the height of the catalogue beside it, leaving space.
- Agent run-log lines are monospace and wrap at narrow widths; on a phone the label sits above
  each line rather than beside it.
