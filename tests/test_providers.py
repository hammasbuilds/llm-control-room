import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llm_control_room.providers import (
    AnthropicProvider,
    ChatRequest,
    MockProvider,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderError,
    ProviderSet,
    count_tokens,
    mock_directives,
    parse_specs,
)


def req(text, **kw):
    return ChatRequest([{"role": "user", "content": text}], **kw)


def test_mock_is_deterministic_per_seed():
    a, b = MockProvider(1), MockProvider(1)
    ra = a.complete(req("How do I configure retries?"), "swift-mock")
    rb = b.complete(req("How do I configure retries?"), "swift-mock")
    assert ra.text == rb.text and ra.latency_ms == rb.latency_ms and ra.quality_ok == rb.quality_ok


def test_mock_models_order_by_price_latency_quality():
    models = MockProvider().models()
    assert [m.usd_out for m in models] == sorted(m.usd_out for m in models)
    assert [m.quality[2] for m in models] == sorted(m.quality[2] for m in models)
    assert [m.base_ms for m in models] == sorted(m.base_ms for m in models)


def test_usage_and_cost():
    r = MockProvider().complete(req("hello there"), "nano-mock")
    assert r.prompt_tokens == count_tokens("hello there") and r.completion_tokens > 0
    info = MockProvider().models()[0]
    assert info.cost(1_000_000, 0) == pytest.approx(info.usd_in)


def test_max_tokens_truncates():
    r = MockProvider().complete(req("Summarise: " + "word " * 200, max_tokens=5), "nano-mock")
    assert r.completion_tokens <= 5


def test_unknown_model():
    with pytest.raises(ProviderError):
        MockProvider().complete(req("x"), "nope")


def test_fault_injection_errors_and_latency():
    p = MockProvider()
    p.set_fault("swift-mock", error_rate=1.0)
    with pytest.raises(ProviderError):
        p.complete(req("hi"), "swift-mock")
    p.set_fault("swift-mock", latency_mult=5)
    slow = p.complete(req("hi"), "swift-mock").latency_ms
    p.clear_faults()
    assert slow > 3 * p.complete(req("hi"), "swift-mock").latency_ms


def test_directive_lowers_quality():
    assert mock_directives("x [[mock quality=-0.5 latency=2]]") == {"quality": -0.5, "latency": 2.0}
    p = MockProvider(3)
    good = sum(
        p.complete(req(f"q{i}", true_difficulty=0), "titan-mock").quality_ok for i in range(200)
    )
    system = {"role": "system", "content": "[[mock quality=-0.9]]"}
    bad = sum(
        p.complete(
            ChatRequest([system, {"role": "user", "content": f"q{i}"}], true_difficulty=0),
            "titan-mock",
        ).quality_ok
        for i in range(200)
    )
    assert good > 180 and bad < 40


def test_grounded_answer_comes_from_context():
    r = MockProvider().complete(
        req(
            "How long do refunds take?",
            context="Refunds take 5 to 7 business days. Shipping is free.",
        ),
        "titan-mock",
    )
    assert "5 to 7" in r.text


def test_parse_specs_handles_ollama_tags():
    m = parse_specs("qwen2.5:0.5b, gpt-x:0.15:0.6:0.9:0.8:0.5", "x")
    assert m[0].id == "qwen2.5:0.5b" and m[0].usd_in == 0
    assert m[1].id == "gpt-x" and m[1].usd_out == 0.6 and m[1].quality == (0.9, 0.8, 0.5)


def test_providerset_is_mock_only_by_default_and_reads_env():
    assert [p.name for p in ProviderSet(env={}).providers] == ["mock"]
    ps = ProviderSet(
        env={
            "LCR_OLLAMA_MODELS": "tiny:0",
            "LCR_OPENAI_MODELS": "m:1:2",
            "LCR_ANTHROPIC_MODELS": "c:1:5",
            "ANTHROPIC_API_KEY": "k",
        }
    )
    assert {p.name for p in ps.providers} == {"mock", "ollama", "openai", "anthropic"}
    assert ps.provider_for("tiny").name == "ollama"
    # an Anthropic model list without a key is ignored rather than failing
    assert "anthropic" not in [
        p.name for p in ProviderSet(env={"LCR_ANTHROPIC_MODELS": "c"}).providers
    ]


class Stub(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.path.endswith("/chat/completions"):
            out = {
                "choices": [{"message": {"content": "openai says hi"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        elif self.path.endswith("/api/chat"):
            out = {
                "message": {"content": "ollama says hi"},
                "prompt_eval_count": 5,
                "eval_count": 2,
            }
        elif self.path.endswith("/v1/messages"):
            out = {
                "content": [{"type": "text", "text": "claude says hi"}],
                "usage": {"input_tokens": 9, "output_tokens": 4},
            }
        else:
            self.send_response(500)
            self.end_headers()
            return
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Stub.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_openai_compatible_provider(stub):
    p = OpenAICompatProvider(stub + "/v1", "secret", parse_specs("m:1:2", "openai"))
    c = p.complete(req("hi"), "m")
    assert c.text == "openai says hi" and (c.prompt_tokens, c.completion_tokens) == (7, 3)
    assert Stub.seen[0][1]["authorization"] == "Bearer secret"


def test_ollama_and_anthropic_providers(stub):
    assert OllamaProvider(stub).complete(req("hi"), "m").text == "ollama says hi"
    c = AnthropicProvider("key", base_url=stub).complete(
        ChatRequest([{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]),
        "c",
    )
    assert c.text == "claude says hi" and c.completion_tokens == 4
    _, headers, body = Stub.seen[-1]
    assert headers["x-api-key"] == "key" and body["system"] == "be brief"


def test_real_provider_failure_is_a_provider_error():
    with pytest.raises(ProviderError):
        OpenAICompatProvider("http://127.0.0.1:1/v1", "", [], timeout=1).complete(req("hi"), "m")


def test_mock_reply_is_a_readable_labelled_sentence_not_a_token_list():
    """The owner's review found "On card, credit, declined, email, redacted: ...": the mock
    must restate the topic in words and name the model tier, never list stopwords or tokens."""
    q = "My email is [REDACTED_EMAIL] and card [REDACTED_CREDIT_CARD] was declined, why?"
    p = MockProvider(3)
    texts = {p.complete(req(q, true_difficulty=d), m).text
             for m in ("nano-mock", "titan-mock") for d in (0, 2) for _ in range(5)}
    for t in texts:
        assert "REDACTED" not in t and "redacted" not in t
        assert "(email removed)" in t and "(card number removed)" in t
        assert "card, credit" not in t and "I am not certain, but on" not in t
        assert "mock reply from" in t.lower()
    assert any("tiny tier" in t for t in texts) and any("frontier tier" in t for t in texts)
    # a failed quality draw is labelled as such, in words
    assert any(t.startswith("Low-confidence mock reply from") for t in texts)


def test_mock_topic_shortens_long_questions_at_a_word_boundary():
    from llm_control_room.providers import mock_topic

    long_q = ("A train leaves city A at 60 km/h and another leaves city B, 300 km away, "
              "at 90 km/h toward it. Work out where they meet.")
    t = mock_topic(long_q)
    assert t.endswith("...") and "Work out" not in t and len(t.split()) <= 14
    assert mock_topic("Use this key [REDACTED_OPENAI_KEY] now") == "Use this key (key removed) now"
    assert mock_topic("   ") == ""
    # the card pattern eats the space after the number; the restated topic keeps the words apart
    assert mock_topic("card [REDACTED_CREDIT_CARD]was declined") == "card (card number removed) was declined"


def test_max_tokens_cuts_on_a_word_boundary():
    r = MockProvider().complete(req("Design a migration plan for the billing service", max_tokens=20),
                                "nano-mock")
    assert r.text.endswith("...") and r.completion_tokens <= 20
    body = r.text[:-3]
    full = MockProvider().complete(req("Design a migration plan for the billing service"),
                                   "nano-mock").text
    assert full.startswith(body) and full[len(body)] == " "
