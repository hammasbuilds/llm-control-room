"""Input and output screening that sits in front of the vendored guardrails.

The vendored ``_vendor/guardrails.py`` is a faithful copy of another project and is kept
unmodified. It matches the ASCII spelling of a secret or an injection phrase, which is exactly
what an evasion avoids. This module adds, in front of it:

* canonicalisation: NFKC (full-width letters and digits, ligatures) and removal of invisible
  format characters (zero-width space/joiner, bidi controls, soft hyphen, tag characters) before
  anything is matched, so ``sk-`` split by U+200B is still a key;
* look-alike folding for injection detection (Cyrillic and Greek letters that render as Latin),
  and a compact form with spacing and punctuation removed (``i g n o r e ...``);
* one level of decoding for base64, percent-encoding and hex tokens, so an encoded key or an
  encoded instruction is found;
* more secret shapes (``sk-proj-`` keys, Stripe, Google, Hugging Face, bearer tokens, whole PEM
  blocks rather than only the header line, ``password=...`` assignments, this gateway's own keys)
  and a few more PII shapes;
* bounded quantifiers everywhere, so a hostile 400 KB message cannot stall a regex.

Blocking injection stays a speed bump, not a boundary: a paraphrase in another language gets
through, and the README says so. Redaction is the control that holds.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from urllib.parse import unquote

from ._vendor.guardrails import INJECTION_PATTERNS as _VENDOR_INJECTION
from ._vendor.guardrails import GuardResult

# ----------------------------------------------------------------------------- patterns

_PEM = (
    r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[\s\S]{0,8000}?-----END [A-Z ]{0,30}PRIVATE KEY-----"
    r"|-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,3000}"
)

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(_PEM)),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("lcr_key", re.compile(r"\blcr-[A-Za-z0-9_\-]{8,64}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s{1,4}[A-Za-z0-9._~+/=\-]{20,}")),
    (
        "credential",
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)"
            r"[a-z0-9_.\-]{0,20}[\"']?[ \t]{0,3}[:=][ \t]{0,3}[\"']?[^\s\"',;]{8,200}"
        ),
    ),
]

PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[\w.%+\-]{1,64}@[\w.\-]{1,255}\.[A-Za-z]{2,24}\b")),
    ("cnic", re.compile(r"\b\d{5}-\d{7}-\d\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone_pk", re.compile(r"\b(?:\+92|0)3\d{2}[- ]?\d{7}\b")),
    (
        "phone_intl",
        re.compile(r"(?<![\w+])\+\d{1,3}[ \-]?\(?\d{2,4}\)?[ \-]?\d{3,4}[ \-]?\d{3,4}\b"),
    ),
    ("credit_card", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
]

_INJ_EXTRA: list[tuple[str, re.Pattern]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|the\s+|your\s+|"
            r"every\s+|my\s+|these\s+|those\s+){0,3}(?:previous|prior|above|earlier|preceding|"
            r"former|initial|original|system|safety)\s+(?:instructions?|prompts?|rules|directives?|"
            r"guidelines|context|constraints|restrictions)\b",
            re.I,
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"\bforget\s+(?:everything|all)\s+(?:above|before|you\s+(?:were|have\s+been)\s+told)\b",
            re.I,
        ),
    ),
    (
        "role_override",
        re.compile(r"\b(?:do\s+anything\s+now|jailbroken|unrestricted\s+mode)\b", re.I),
    ),
    (
        "system_prompt_exfil",
        re.compile(
            r"\b(?:reveal|repeat|print|show|output|display|leak|dump|disclose|tell\s+me|give\s+me)\s+"
            r"(?:me\s+)?(?:your|the)\s+(?:(?:initial|original|hidden|secret|full)\s+)?"
            r"(?:system\s+prompt|system\s+message|instructions|rules)\b",
            re.I,
        ),
    ),
    (
        "system_prompt_exfil",
        re.compile(r"\bwhat\s+(?:is|are)\s+your\s+(?:system\s+prompt|instructions|rules)\b", re.I),
    ),
    ("delimiter_injection", re.compile(r"<\|[a-z_]{2,24}\|>|<<\s*/?\s*sys\s*>>|\[/?inst\]", re.I)),
]
INJECTION_PATTERNS = [*_VENDOR_INJECTION, *_INJ_EXTRA]

# phrases checked on the compact form (letters and digits only), for "i g n o r e" style spacing
_COMPACT = re.compile(
    r"(?:ignore|disregard|forget|override)(?:all|any|the|your|every|my)*"
    r"(?:previous|prior|above|earlier|preceding|system)(?:instructions?|prompts?|rules|directives?)"
    r"|(?:reveal|print|show|repeat|leak)(?:your|the)(?:system)?(?:prompt|instructions)"
    r"|youarenow(?:a|an|in)(?:dan|developer|god|jailbreak)"
)

# ----------------------------------------------------------------------------- canonical forms

# Cyrillic and Greek letters that render like Latin ones (text is lower-cased first)
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "ԛ": "q",
        "ɡ": "g",
        "ο": "o",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ı": "i",
        "ӏ": "l",
    }
)


def canon(text: str) -> str:
    """NFKC, minus invisible format characters (category Cf: zero-width, bidi, tags, BOM)."""
    t = unicodedata.normalize("NFKC", text)
    return "".join(c for c in t if unicodedata.category(c) != "Cf")


def fold(text: str) -> str:
    """Lower case, look-alike letters mapped to Latin, combining marks dropped."""
    t = unicodedata.normalize("NFD", canon(text).lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.translate(_HOMOGLYPHS)


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(text))


# ----------------------------------------------------------------------------- encoded tokens

_TOKEN = re.compile(r"[A-Za-z0-9+/_\-%=]{16,2000}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}){10,}")


def _printable(raw: bytes) -> str | None:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s or sum(ch.isprintable() or ch in "\n\r\t" for ch in s) / len(s) < 0.95:
        return None
    return s


def _decodings(tok: str) -> list[str]:
    out: list[str] = []
    if "%" in tok:
        out.append(unquote(tok))
    if _HEX.fullmatch(tok):
        s = _printable(bytes.fromhex(tok))
        if s:
            out.append(s)
    body = tok.rstrip("=")
    if len(body) >= 16 and "%" not in body:
        padded = body + "=" * (-len(body) % 4)
        for fn in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                s = _printable(fn(padded))
            except (binascii.Error, ValueError):
                continue
            if s:
                out.append(s)
                break
    return out


def _scan(text: str, patterns) -> list[str]:
    return [label for label, p in patterns if p.search(text)]


# ----------------------------------------------------------------------------- public API


def injection_findings(text: str) -> list[str]:
    """Names of the injection patterns the text matches, by any of its canonical forms."""
    views = {text, canon(text), fold(text)}
    found: list[str] = []
    for v in views:
        for label, pat in INJECTION_PATTERNS:
            if pat.search(v):
                found.append(f"injection:{label}")
    if _COMPACT.search(compact(text)):
        found.append("injection:instruction_override")
    for tok in _TOKEN.findall(canon(text)):
        for dec in _decodings(tok):
            f2 = {lab for v in (dec, fold(dec)) for lab, p in INJECTION_PATTERNS if p.search(v)}
            if f2 or _COMPACT.search(compact(dec)):
                found.append("injection:encoded")
    return sorted(set(found))


def _redact(text: str, patterns, findings: list[str]) -> str:
    for label, pat in patterns:
        if pat.search(text):
            findings.append(f"redacted:{label}")
            text = pat.sub(f"[REDACTED_{label.upper()}]", text)
    return text


def _redact_encoded(text: str, patterns, findings: list[str]) -> str:
    def sub(m: re.Match) -> str:
        for dec in _decodings(m.group(0)):
            hit = _scan(dec, patterns)
            if hit:
                findings.append(f"redacted:encoded_{hit[0]}")
                return f"[REDACTED_ENCODED_{hit[0].upper()}]"
        return m.group(0)

    return _TOKEN.sub(sub, text)


def term_findings(text: str, deny_terms) -> list[str]:
    """``policy:deny_term`` when any of a tenant's literal terms appears (any case, any look-alike)."""
    if not deny_terms:
        return []
    f = fold(text)
    return ["policy:deny_term"] if any(fold(t) and fold(t) in f for t in deny_terms) else []


def screen(
    text: str,
    *,
    redact_pii: bool = False,
    check_injection: bool = True,
    deny_terms=(),
    redact_terms=(),
) -> GuardResult:
    """Block injection (when asked) and redact secrets, plus PII when the tenant wants it.

    ``deny_terms`` block the request and ``redact_terms`` are replaced; both are literal strings
    (never patterns, so a tenant cannot supply a regex that stalls the gateway).

    The text is returned unchanged unless something was redacted, so ordinary Urdu or Persian
    text (which legitimately contains U+200C) is never rewritten for no reason.
    """
    if check_injection:
        inj = injection_findings(text)
        if inj:
            return GuardResult(allowed=False, text=text, findings=inj)
    hit = term_findings(text, deny_terms)
    if hit:
        return GuardResult(allowed=False, text=text, findings=hit)
    view = canon(text)
    findings: list[str] = []
    if redact_terms:
        pat = re.compile("|".join(re.escape(t) for t in redact_terms if t), re.I)
        if pat.search(view):
            findings.append("redacted:custom_term")
            view = pat.sub("[REDACTED_TERM]", view)
    patterns = SECRET_PATTERNS + (PII_PATTERNS if redact_pii else [])
    red = _redact(view, SECRET_PATTERNS, findings)
    if redact_pii:
        red = _redact(red, PII_PATTERNS, findings)
    red = _redact_encoded(red, patterns, findings)
    if not findings:
        return GuardResult(allowed=True, text=text, findings=[])
    return GuardResult(allowed=True, text=red, findings=sorted(set(findings)))


def screen_output(text: str, *, redact_pii: bool = False, redact_terms=()) -> GuardResult:
    """Applied to what the model said: it may echo a secret it saw elsewhere."""
    return screen(text, redact_pii=redact_pii, check_injection=False, redact_terms=redact_terms)
