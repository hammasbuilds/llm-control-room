"""Block terms refuse a request; redact terms only mask. Pinned through the HTTP API."""

import pytest

BLOCK, REDACT = "project orion", "zephyr"


@pytest.fixture()
def tenant(client):
    t = client.post(
        "/api/tenants",
        json={"name": "termco", "budget_usd": 5.0, "deny_terms": [BLOCK], "redact_terms": [REDACT]},
    ).json()
    return {"Authorization": "Bearer " + t["first_key"]["key"]}


def _play(client, prompt):
    return client.post("/api/playground", json={"tenant": "termco", "prompt": prompt})


def _chat(client, headers, prompt):
    return client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": prompt}]},
    )


REDACT_ONLY = [
    "Draft a status note about the zephyr contract.",
    "Draft a status note about the ZEPHYR contract.",
    "Draft a status note about the Zephyr   contract.",
    "zephyr",
    "  ZePhYr\n",
]
BLOCK_ONLY = [
    "Tell me about project orion",
    "Tell me about PROJECT ORION",
    "Tell me about project   orion",
    "Tell me about Project\torion",
]
BOTH = ["project orion and the zephyr contract", "ZEPHYR and PROJECT  ORION"]


@pytest.mark.parametrize("prompt", REDACT_ONLY)
def test_redact_term_alone_is_served_masked(client, tenant, prompt):
    r = _play(client, prompt)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b.get("text") or b.get("choices") or b.get("error") is None, b
    assert "redacted:custom_term" in b.get("redactions", []), b
    assert "zephyr" not in str(b.get("text", "")).lower()
    c = _chat(client, tenant, prompt)
    assert c.status_code == 200, c.text
    assert "zephyr" not in c.text.lower()


@pytest.mark.parametrize("prompt", BLOCK_ONLY + BOTH)
def test_block_term_refuses(client, tenant, prompt):
    r = _chat(client, tenant, prompt)
    assert r.status_code == 400, r.text
    assert "policy:deny_term" in r.text
    p = _play(client, prompt)
    assert (
        "policy:deny_term" in p.text
        and "text" not in p.json().get("text", "x")
        or p.status_code >= 400
    )


def test_multiword_redact_term_survives_whitespace_and_case(client):
    client.post("/api/tenants", json={"name": "mw", "redact_terms": ["acme internal"]})
    res = client.post(
        "/api/playground/batch",
        json={"tenant": "mw", "prompts": [{"prompt": "see ACME   internal notes"}]},
    ).json()["rows"][0]
    assert res["status"] != "blocked" and res["redactions"]
