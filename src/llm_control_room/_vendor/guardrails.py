# Vendored copy. Source: llm-gateway/src/gateway/policy/guardrails.py (commit 0cc9128), MIT licence, (c) 2026 Muhammad Hammas. Unmodified.
"""Input and output guardrails.

Two jobs, deliberately separate:

  **Input**  - block prompt-injection attempts before they reach a model, and redact
               secrets before they leave the building.
  **Output** - stop the model returning a secret that was in its context.

The redaction direction matters more than the blocking one. Blocking injection is a
losing arms race; not forwarding an API key to a third party is a control that
actually holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered most-specific first, so a Stripe key is not merely tagged as a generic token.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
]

# Order matters: patterns are applied in sequence and the first to match claims the
# text, so the specific ones must precede the general one. A CNIC is 13 digits with
# separators, which the credit-card pattern also matches - leaving it second would
# redact a national ID and report it to a compliance audit as a card number.
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    # Pakistani CNIC: 5 digits - 7 digits - 1 digit.
    ("cnic", re.compile(r"\b\d{5}-\d{7}-\d\b")),
    ("phone_pk", re.compile(r"\b(?:\+92|0)3\d{2}[- ]?\d{7}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
]

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "instruction_override",
        re.compile(
            r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)\b", re.I
        ),
    ),
    ("role_override", re.compile(r"\byou\s+are\s+now\s+(a|an|in)\b", re.I)),
    (
        "system_prompt_exfil",
        re.compile(
            r"\b(reveal|repeat|print|show|output)\s+(your|the)\s+"
            r"(system\s+prompt|instructions|rules)\b",
            re.I,
        ),
    ),
    ("delimiter_injection", re.compile(r"(<\|im_start\|>|<\|endoftext\|>|\[INST\])", re.I)),
    ("developer_mode", re.compile(r"\b(dev|developer|god|jailbreak)\s*mode\b", re.I)),
]


@dataclass
class GuardResult:
    allowed: bool
    text: str
    findings: list[str]

    @property
    def redacted(self) -> bool:
        return any(f.startswith("redacted:") for f in self.findings)


def _redact(text: str, patterns: list[tuple[str, re.Pattern]], findings: list[str]) -> str:
    for label, pattern in patterns:
        if pattern.search(text):
            findings.append(f"redacted:{label}")
            text = pattern.sub(f"[REDACTED_{label.upper()}]", text)
    return text


def check_input(
    text: str,
    *,
    redact_secrets: bool = True,
    redact_pii: bool = False,
    block_injection: bool = True,
) -> GuardResult:
    findings: list[str] = []

    if block_injection:
        for label, pattern in INJECTION_PATTERNS:
            if pattern.search(text):
                findings.append(f"injection:{label}")
        if findings:
            # Blocked rather than redacted: an injection attempt rewritten into
            # something harmless-looking is worse than a refusal, because it hides
            # that anyone tried.
            return GuardResult(allowed=False, text=text, findings=findings)

    if redact_secrets:
        text = _redact(text, SECRET_PATTERNS, findings)
    if redact_pii:
        text = _redact(text, PII_PATTERNS, findings)

    return GuardResult(allowed=True, text=text, findings=findings)


def check_output(text: str, *, redact_secrets: bool = True) -> GuardResult:
    """Applied to the model's reply.

    A model can echo a secret that reached it another way - from a retrieved document,
    or from its own training data. The output path is the last place to catch that.
    """
    findings: list[str] = []
    if redact_secrets:
        text = _redact(text, SECRET_PATTERNS, findings)
    return GuardResult(allowed=True, text=text, findings=findings)
