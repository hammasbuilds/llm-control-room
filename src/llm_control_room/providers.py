"""Model providers: a deterministic mock (always available) and optional real ones.

The mock provider is the reason every chart and feature in the control room works offline.
It has four fake models with different price, latency and quality profiles. Everything it
returns is a pure function of the request (plus a seeded generator for injected faults), so
the same prompt gives the same text, the same token counts and the same success/failure draw.

Mock latency is *simulated*: it is computed, reported and stored, but nobody sleeps for it.
Mock prices are invented round numbers that only keep the ratios of a real price list.

Real providers are opt-in through environment variables and never required:

    LCR_OLLAMA_MODELS=qwen2.5:0.5b            (LCR_OLLAMA_URL, default http://127.0.0.1:11434)
    LCR_OPENAI_MODELS=gpt-4o-mini:0.15:0.6    (LCR_OPENAI_BASE_URL, LCR_OPENAI_API_KEY)
    LCR_ANTHROPIC_MODELS=claude-haiku-4-5:1:5 (ANTHROPIC_API_KEY)

A model spec is ``id[:usd_in_per_1M[:usd_out_per_1M[:q_easy:q_medium:q_hard]]]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class ProviderError(RuntimeError):
    """An upstream failure: the provider is down, slow, rate limiting or rejecting."""

    def __init__(self, message: str, kind: str = "upstream") -> None:
        super().__init__(message)
        self.kind = kind


def count_tokens(text: str) -> int:
    """Four characters per token. Crude, identical for every model, and good enough for
    attribution; real providers report their own usage and override it."""
    return max(1, round(len(text) / 4)) if text else 0


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    usd_in: float  # per million input tokens
    usd_out: float  # per million output tokens
    base_ms: float  # typical time to first token
    ms_per_token: float  # typical decode speed
    quality: tuple[float, float, float]  # chance a good answer for easy / medium / hard work
    mock: bool = False
    description: str = ""

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.usd_in + completion_tokens * self.usd_out) / 1_000_000

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "usd_in_per_1m": self.usd_in,
            "usd_out_per_1m": self.usd_out,
            "base_ms": self.base_ms,
            "ms_per_token": self.ms_per_token,
            "quality": {
                "easy": self.quality[0],
                "medium": self.quality[1],
                "hard": self.quality[2],
            },
            "mock": self.mock,
            "description": self.description,
        }


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    # None when nobody can know (a real model); True/False for the mock's own draw.
    quality_ok: bool | None = None


@dataclass
class ChatRequest:
    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.0
    # Extensions carried by the gateway. Only the mock reads true_difficulty.
    context: str = ""
    true_difficulty: int | None = None  # 0 easy, 1 medium, 2 hard
    estimated_difficulty: int = 1

    @property
    def system(self) -> str:
        return "\n".join(m.get("content", "") for m in self.messages if m.get("role") == "system")

    @property
    def user(self) -> str:
        users = [m.get("content", "") for m in self.messages if m.get("role") == "user"]
        return users[-1] if users else ""

    @property
    def prompt_text(self) -> str:
        return "\n".join(str(m.get("content", "")) for m in self.messages)


MOCK_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "nano-mock",
        "mock",
        0.10,
        0.30,
        90,
        3.0,
        (0.93, 0.55, 0.20),
        True,
        "Tiny and very cheap. Fine for greetings and lookups, poor at reasoning.",
    ),
    ModelInfo(
        "swift-mock",
        "mock",
        0.50,
        1.50,
        200,
        7.0,
        (0.96, 0.84, 0.45),
        True,
        "Small and fast. Good for routine work.",
    ),
    ModelInfo(
        "sage-mock",
        "mock",
        3.00,
        9.00,
        520,
        14.0,
        (0.98, 0.94, 0.78),
        True,
        "Mid-size. Reliable on most things, slips on the hardest.",
    ),
    ModelInfo(
        "titan-mock",
        "mock",
        15.00,
        45.00,
        1300,
        28.0,
        (0.99, 0.97, 0.93),
        True,
        "Frontier-class. Best quality, five to fifty times the price.",
    ),
)

_DIRECTIVE = re.compile(r"\[\[mock\s+([^\]]*)\]\]", re.I)


def mock_directives(system: str) -> dict[str, float]:
    """``[[mock quality=-0.4 latency=1.8 error=0.1]]`` in a system prompt simulates a bad
    prompt version offline: it lowers the success chance, scales latency, injects errors."""
    out: dict[str, float] = {}
    for m in _DIRECTIVE.finditer(system or ""):
        for pair in m.group(1).split():
            if "=" in pair:
                k, _, v = pair.partition("=")
                try:
                    out[k.lower()] = float(v)
                except ValueError:
                    pass
    return out


def _h(*parts: str) -> float:
    """A stable number in [0, 1) from the parts."""
    d = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return int.from_bytes(d[:8], "big") / float(1 << 64)


_SENT = re.compile(r"(?<=[.!?])\s+")
_WORDS = re.compile(r"[a-z0-9']+")
_STOP = set(
    "the a an of to in on for and or is are was were be it this that with as at by from "
    "what who when where why how which do does did can could should would will i you we "
    "they my your our please tell me about give explain".split()
)


def content_words(text: str) -> set[str]:
    return {w for w in _WORDS.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _grounded_answer(question: str, context: str, hallucinate: bool, seed: str) -> str:
    sentences = [s.strip() for s in _SENT.split(context) if s.strip()]
    q = content_words(question)
    ranked = sorted(sentences, key=lambda s: -len(q & content_words(s)))
    picked = [s for s in ranked[:2] if q & content_words(s)] or ranked[:1]
    text = " ".join(picked) if picked else "The provided context does not say."
    if hallucinate:
        fabrications = [
            "It was formally approved by the regional board in 2019.",
            "Independent auditors confirmed a figure of 87 percent last quarter.",
            "The original design was later replaced by a cheaper alternative.",
        ]
        text += " " + fabrications[int(_h(seed) * len(fabrications))]
    return text


def _canned_answer(req: ChatRequest, seed: str) -> str:
    q = req.user.strip()
    low = q.lower()
    if re.match(r"^\s*(hi|hello|hey|thanks|thank you)\b", low):
        return "Hello! How can I help you today?"
    if "```" in q or re.search(r"\b(function|refactor|implement|bug|code)\b", low):
        return (
            "Here is an approach.\n```python\ndef solve(items):\n    seen = set()\n"
            "    return [x for x in items if not (x in seen or seen.add(x))]\n```\n"
            "It keeps the first occurrence of each item in order."
        )
    if re.search(r"\b(summari[sz]e|tl;?dr)\b", low):
        body = _SENT.split(q.split(":", 1)[-1].strip())[0][:240]
        return f"Summary: {body}"
    if re.search(r"\b(classify|categori[sz]e|label|sentiment)\b", low):
        labels = ["billing", "bug report", "feature request", "praise", "complaint"]
        return f"Label: {labels[int(_h(seed, 'label') * len(labels))]}"
    if re.search(r"\b(translate)\b", low):
        return "Translation: " + q.split(":", 1)[-1].strip()[:160]
    words = sorted(content_words(q))[:6]
    topic = ", ".join(words) if words else "that"
    return (
        f"On {topic}: the short answer depends on the details you give, but the usual "
        "approach is to state the goal, list the constraints, and check the result against "
        "them before committing."
    )


class MockProvider:
    """Deterministic fake models plus per-model fault injection."""

    name = "mock"

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.qrng = random.Random(seed + 1)  # success draws: sampled, but reproducible per seed
        # model id -> {"error_rate": 0..1, "latency_mult": x}
        self.faults: dict[str, dict[str, float]] = {}

    def models(self) -> list[ModelInfo]:
        return list(MOCK_MODELS)

    def reseed(self, seed: int) -> None:
        """Restart the generators, so a scenario with the same seed replays identically."""
        self.rng = random.Random(seed)
        self.qrng = random.Random(seed + 1)

    def set_fault(self, model: str, *, error_rate: float = 0.0, latency_mult: float = 1.0) -> None:
        if error_rate <= 0 and latency_mult == 1.0:
            self.faults.pop(model, None)
        else:
            self.faults[model] = {"error_rate": error_rate, "latency_mult": latency_mult}

    def clear_faults(self) -> None:
        self.faults.clear()

    def complete(self, req: ChatRequest, model: str) -> Completion:
        info = next((m for m in MOCK_MODELS if m.id == model), None)
        if info is None:
            raise ProviderError(f"unknown mock model {model!r}", kind="bad_request")
        fault = self.faults.get(model, {})
        d = mock_directives(req.system)
        err = max(fault.get("error_rate", 0.0), d.get("error", 0.0))
        if err > 0 and self.rng.random() < err:
            raise ProviderError(f"{model}: 503 service unavailable (injected)")

        text_in = req.prompt_text
        seed = f"{model}|{req.system}|{req.user}|{req.context}"
        level = req.true_difficulty if req.true_difficulty is not None else req.estimated_difficulty
        p_ok = min(1.0, max(0.0, info.quality[level] + d.get("quality", 0.0)))
        ok = self.qrng.random() < p_ok
        if req.context:
            text = _grounded_answer(req.user, req.context, hallucinate=not ok, seed=seed)
        else:
            text = _canned_answer(req, seed)
            if not ok:
                text = "I am not certain, but " + text[0].lower() + text[1:]

        out_tokens = min(req.max_tokens, count_tokens(text))
        if out_tokens < count_tokens(text):
            text = text[: out_tokens * 4]
        jitter = 0.85 + 0.3 * _h(seed, "jitter")
        spike = 3.0 if _h(seed, "spike") < 0.03 else 1.0
        latency = (info.base_ms + out_tokens * info.ms_per_token) * jitter * spike
        latency *= fault.get("latency_mult", 1.0) * max(0.1, d.get("latency", 1.0))
        return Completion(text, count_tokens(text_in), out_tokens, round(latency, 1), ok)


# --------------------------------------------------------------------------- real providers


def parse_specs(raw: str, provider: str, default_quality=(0.9, 0.8, 0.6)) -> list[ModelInfo]:
    out = []
    for spec in filter(None, (s.strip() for s in raw.split(","))):
        parts = spec.split(":")
        # ollama ids contain a colon (qwen2.5:0.5b); a numeric tail is a price, not a tag
        i = 1
        name = parts[0]
        while i < len(parts) and not _is_num(parts[i]):
            name += ":" + parts[i]
            i += 1
        nums = [float(x) for x in parts[i:] if _is_num(x)]
        usd_in = nums[0] if len(nums) > 0 else 0.0
        usd_out = nums[1] if len(nums) > 1 else usd_in
        q = tuple(nums[2:5]) if len(nums) >= 5 else default_quality
        out.append(
            ModelInfo(
                name,
                provider,
                usd_in,
                usd_out,
                800,
                25,
                q,
                False,
                f"{provider} model from environment",
            )
        )
    return out


def _is_num(x: str) -> bool:
    try:
        float(x)
        return True
    except ValueError:
        return False


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError(f"cannot reach {url}: {exc}") from exc


@dataclass
class OpenAICompatProvider:
    base_url: str
    api_key: str = ""
    specs: list[ModelInfo] = field(default_factory=list)
    name: str = "openai"
    timeout: float = 60.0

    def models(self) -> list[ModelInfo]:
        return self.specs

    def complete(self, req: ChatRequest, model: str) -> Completion:
        started = time.perf_counter()
        h = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = _post(
            self.base_url.rstrip("/") + "/chat/completions",
            {
                "model": model,
                "messages": req.messages,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
            },
            h,
            self.timeout,
        )
        try:
            text = r["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("malformed OpenAI-style response") from exc
        u = r.get("usage") or {}
        return Completion(
            text,
            u.get("prompt_tokens") or count_tokens(req.prompt_text),
            u.get("completion_tokens") or count_tokens(text),
            round((time.perf_counter() - started) * 1000, 1),
        )


@dataclass
class OllamaProvider:
    url: str = "http://127.0.0.1:11434"
    specs: list[ModelInfo] = field(default_factory=list)
    name: str = "ollama"
    timeout: float = 300.0

    def models(self) -> list[ModelInfo]:
        return self.specs

    def complete(self, req: ChatRequest, model: str) -> Completion:
        started = time.perf_counter()
        r = _post(
            self.url.rstrip("/") + "/api/chat",
            {
                "model": model,
                "messages": req.messages,
                "stream": False,
                "options": {"num_predict": req.max_tokens, "temperature": req.temperature},
            },
            {},
            self.timeout,
        )
        text = (r.get("message") or {}).get("content", "")
        return Completion(
            text,
            r.get("prompt_eval_count") or count_tokens(req.prompt_text),
            r.get("eval_count") or count_tokens(text),
            round((time.perf_counter() - started) * 1000, 1),
        )


@dataclass
class AnthropicProvider:
    api_key: str
    specs: list[ModelInfo] = field(default_factory=list)
    base_url: str = "https://api.anthropic.com"
    name: str = "anthropic"
    timeout: float = 120.0

    def models(self) -> list[ModelInfo]:
        return self.specs

    def complete(self, req: ChatRequest, model: str) -> Completion:
        started = time.perf_counter()
        msgs = [m for m in req.messages if m.get("role") in ("user", "assistant")]
        body = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": msgs,
            "temperature": req.temperature,
        }
        if req.system:
            body["system"] = req.system
        r = _post(
            self.base_url.rstrip("/") + "/v1/messages",
            body,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            self.timeout,
        )
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage") or {}
        return Completion(
            text,
            u.get("input_tokens") or count_tokens(req.prompt_text),
            u.get("output_tokens") or count_tokens(text),
            round((time.perf_counter() - started) * 1000, 1),
        )


class ProviderSet:
    """Every provider the control room can reach, found from the environment."""

    def __init__(self, env: dict[str, str] | None = None, mock_seed: int = 7) -> None:
        env = os.environ if env is None else env
        self.mock = MockProvider(mock_seed)
        self.providers: list = [self.mock]
        if env.get("LCR_OLLAMA_MODELS"):
            self.providers.append(
                OllamaProvider(
                    env.get("LCR_OLLAMA_URL", "http://127.0.0.1:11434"),
                    parse_specs(env["LCR_OLLAMA_MODELS"], "ollama"),
                )
            )
        if env.get("LCR_OPENAI_MODELS"):
            self.providers.append(
                OpenAICompatProvider(
                    env.get("LCR_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    env.get("LCR_OPENAI_API_KEY", ""),
                    parse_specs(env["LCR_OPENAI_MODELS"], "openai"),
                )
            )
        if env.get("LCR_ANTHROPIC_MODELS") and env.get("ANTHROPIC_API_KEY"):
            self.providers.append(
                AnthropicProvider(
                    env["ANTHROPIC_API_KEY"], parse_specs(env["LCR_ANTHROPIC_MODELS"], "anthropic")
                )
            )

    def models(self) -> list[ModelInfo]:
        return [m for p in self.providers for m in p.models()]

    def info(self, model: str) -> ModelInfo | None:
        return next((m for m in self.models() if m.id == model), None)

    def provider_for(self, model: str):
        return next((p for p in self.providers if any(m.id == model for m in p.models())), None)
