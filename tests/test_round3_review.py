"""Round 3, a first-time user's walk through every page: the causes of what read as unfinished
or unclear, pinned so they cannot come back in a plain pytest run. The real-browser check is
scripts/ui_audit.py; the contrast numbers here are computed from the theme tokens."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "llm_control_room" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
INPUTS = (STATIC / "inputs.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def _lum(hexcol: str) -> float:
    c = [int(hexcol[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _tokens(block: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*#([0-9a-f]{6})", block))


def test_no_browser_prompt_for_the_admin_token_or_a_new_key():
    # a window.prompt() said "needs an admin token (LCR_ADMIN_TOKEN)" and nothing else; cancelling
    # it left "Something went wrong". The page now explains where the token file is.
    assert "prompt(" not in APP.replace("prompt(s)", "")
    assert "function showSignIn" in APP
    for where in ("%USERPROFILE%", "~/.llm-control-room/admin-token",
                  "LCR_ADMIN_TOKEN", "That token was not accepted"):
        assert where in APP, where


def test_a_token_pasted_into_an_open_tab_signs_it_in():
    # the #token=... fragment was read once at load; a same-tab hash change ignored it
    route = APP[APP.index("async function route()"):]
    assert "takeTokenFromHash();" in route[:400]


def test_a_new_key_comes_with_the_curl_command_that_uses_it():
    assert "function keyPanel" in APP and "curlText(key)" in APP
    assert "data-copy=" in APP
    # the new-tenant form is the first card, not below every existing tenant
    tenants = APP[APP.index("routes.tenants"):]
    assert tenants.index("<h2>New tenant</h2>") < tenants.index("tenantImportHtml()")
    # the playground no longer shows a placeholder key for a tenant without a demo key
    assert '"lcr-..."' not in APP and "PASTE-THE-KEY-OF-" in APP


def test_the_budget_bar_is_spend_over_budget_not_always_full():
    # bars() scaled one item against itself, so $0.02 of $25 drew a full bar
    assert 'bars([{ label: "budget"' not in APP
    assert "t.spent_usd / t.budget_usd" in APP


def test_a_two_column_grid_has_two_columns():
    # auto-fit made .g2 three columns at 1440 px and left an empty third of the page
    assert ".g2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in CSS
    assert "minmax(380px, 1fr)" not in CSS


def test_tiles_fill_whole_rows():
    assert "cols = n <= 5 ? n : Math.ceil(n / 2)" in APP
    assert '.tiles[style*="--cols"] { grid-template-columns: repeat(var(--cols), minmax(0, 1fr)); }' in CSS


def test_charts_are_drawn_at_their_shown_width():
    # a 640-unit viewBox scaled to 100% printed 10px labels at 5px on a phone, 18px on a wide card
    assert 'width="100%"' not in APP
    assert "const lineChart = (series, opts = {}) => chartSlot(" in APP
    assert "drawCharts(" in APP and 'addEventListener("resize"' in APP


def test_labels_sit_above_fields_instead_of_floating_inside_them():
    assert "transform: translateY(-190%)" not in CSS
    assert ".fl > label { white-space: normal; }" in CSS


def test_jargon_is_explained_where_it_appears():
    for words in ("population stability index", "95 in 100", "two-proportion test",
                  "how much of an answer the retrieved text backs up", "hardening profile (a set of fences",
                  "Shadow</dt>", "Canary</dt>", "SLO means service level objective"):
        assert words in APP, words
    assert 'aboutSection("words", "Words used here"' in INPUTS
    # a p value of 0 printed as "p 0"
    assert "p < 0.001" in APP


def test_about_no_longer_says_a_budget_can_be_overshot():
    # round 2 made the budget a hard cap; the guide still described the old behaviour
    assert "overshot" not in INPUTS


def test_status_text_contrast_has_margin_on_its_tinted_background():
    light = _tokens(CSS.split("@media (prefers-color-scheme: dark)")[0])
    dark = _tokens(CSS[CSS.index(':root[data-theme="dark"]'):])
    for theme in (light, dark):
        for fg, bg in (("good", "good-bg"), ("warn", "warn-bg"), ("bad", "bad-bg")):
            # 11.5 px chip text: pixel measurement put the old light green at 4.26:1
            assert _ratio(theme[fg], theme[bg]) >= 5.0, (fg, theme[fg], theme[bg])
        assert _ratio(theme["accent"], theme["info-bg"]) >= 4.5
        assert _ratio(theme["muted"], theme["panel"]) >= 4.5


def test_scenario_cards_are_buttons():
    assert '<button type="button" class="scen" data-run=' in APP
    assert '<button type="button" class="scen ${s.id === agentSel' in APP
