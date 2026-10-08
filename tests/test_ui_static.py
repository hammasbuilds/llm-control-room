"""Contracts of the static UI that the owner's screenshot review found broken.

The full check is scripts/ui_audit.py in a real browser; these pin the causes so they cannot
come back unnoticed in a plain pytest run.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "llm_control_room" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_event_log_is_full_width_and_never_a_clipped_json_string():
    assert "JSON.stringify(e.detail).slice" not in APP
    assert re.search(r'grid-column:1/-1"><h2>Event log', APP)
    assert "detailHtml(e.detail)" in APP
    assert re.search(r"\.dl dd \{[^}]*overflow-wrap: anywhere", CSS)


def test_tables_scroll_inside_their_card_instead_of_being_clipped():
    # cards no longer clip; every table is wrapped in a .scroll box at render time
    assert ".card { overflow-x: auto; }" not in CSS
    assert "function wrapTables" in APP and "MutationObserver" in APP
    assert re.search(r"\.scroll \{ overflow-x: auto;[^}]*scrollbar-width: thin", CSS)
    # the three cost tables get a grid wide enough for their seven columns
    assert '<div class="grid gt">' in APP and ".gt { grid-template-columns: repeat(auto-fit, minmax(560px, 1fr))" in CSS


def test_phone_nav_is_one_scrolling_row_not_a_wall_of_links():
    blocks = CSS.split("@media (max-width: 860px)")[1:]
    phone = next(b for b in blocks if "nav { position: sticky" in b)
    assert "flex-wrap: nowrap" in phone and "overflow-x: auto" in phone
    assert "nav .brand > span { display: none; }" in phone
    assert "nav .foot { display: block;" in phone  # the theme toggle stays reachable on a phone


def test_release_picker_is_a_link_not_a_button_inside_a_link():
    assert not re.search(r"<a [^>]*href=\"#/releases\?r=[^>]*><button", APP)


def test_busy_spinner_only_on_the_button_just_pressed():
    """The Stop button kept a spinner forever: every disabled button was marked busy."""
    motion = (STATIC / "motion.js").read_text(encoding="utf-8")
    assert 'b.classList.toggle("busy", b.disabled && b === armed' in motion
    assert 'b.classList.toggle("busy", b.disabled);' not in motion


def test_static_assets_are_valid_utf8_with_no_raw_latin1_bytes():
    """motion.js held a raw 0xA0 byte; served as UTF-8 it became U+FFFD and the
    no-break-space check never matched."""
    for f in STATIC.glob("*.*"):
        if f.suffix in (".js", ".css", ".html", ".svg"):
            f.read_bytes().decode("utf-8")  # raises on a stray latin-1 byte


def test_phone_nav_row_does_not_stretch_on_a_short_page():
    """On a page shorter than the screen the grid's min-height split the spare height between
    the nav row and main, so the nav grew to 186 px (Releases with no releases)."""
    blocks = CSS.split("@media (max-width: 860px)")[1:]
    assert any("grid-template-rows: auto 1fr" in b for b in blocks)
