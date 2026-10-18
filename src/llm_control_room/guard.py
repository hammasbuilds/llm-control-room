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
* the usual override phrasing in twelve other languages, and views that undo HTML entities,
  backslash-u escapes, ROT13 and leetspeak;
* redaction of secrets written to dodge a pattern: look-alike letters and dashes, characters
  spaced or dotted out, percent-encoded addresses, and a key split across two messages.

Blocking injection stays a speed bump, not a boundary: a paraphrase, or a language outside that
list, gets through. Redaction is the control that holds.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
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

_DOT = r"(?:\s{0,3}\.\s{0,3}|\s{0,3}[\[\(\{<]\s{0,2}dot\s{0,2}[\]\)\}>]\s{0,3}|\s{1,3}dot\s{1,3})"
_AT_SYM = r"(?:\s{0,3}@\s{0,3}|\s{0,3}[\[\(\{<]\s{0,2}at\s{0,2}[\]\)\}>]\s{0,3})"
# whole words only (a lookbehind and possessive runs), so a 200 KB message is scanned in one pass
# instead of retrying every word from each of its letters
_LOCAL = r"(?<![\w%+\-])[\w%+\-]{1,64}+(?:" + _DOT + r"[\w%+\-]{1,64}+){0,4}"
_DOMAIN = r"[\w\-]{1,63}+(?:" + _DOT + r"[\w\-]{1,63}+){0,4}"
_TLDS = r"(?:com|org|net|edu|gov|io|co|uk|pk|de|fr|in|ai|dev|info|biz|us|ca|au|me|app)"

PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[\w.%+\-]{1,64}@[\w.\-]{1,255}\.[A-Za-z]{2,24}\b")),
    ("cnic", re.compile(r"\b\d{5}-\d{7}-\d\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone_pk", re.compile(r"\b(?:\+92|0)3\d{2}[- ]?\d{7}\b")),
    (
        "phone_intl",
        re.compile(r"(?<![\w+])\+\d{1,3}[ \-]?\(?\d{2,4}\)?[ \-]?\d{3,4}[ \-]?\d{3,4}\b"),
    ),
    # an address written to dodge a filter: "jordan . lee @ example . com", "jordan[at]example[dot]com"
    ("email", re.compile(_LOCAL + _AT_SYM + _DOMAIN + _DOT + r"[A-Za-z]{2,24}\b", re.I)),
    # ... and in words ("jordan dot lee at example dot com"), only before a common top-level domain
    ("email", re.compile(_LOCAL + r"\s{1,3}at\s{1,3}" + _DOMAIN + _DOT + _TLDS + r"\b", re.I)),
    # up to two separators between digits, so "4111  1111  1111  1111" (or dotted) is still a card
    ("credit_card", re.compile(r"\b(?:\d[ \-.]{0,2}){13,19}\b")),
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
# The same override in other languages. The Latin-script ones are written without accents and
# matched on the folded view (accents dropped, dotless i read as i); the rest on the canonical text.
_INJ_LANG: list[tuple[str, re.Pattern]] = [
    ("instruction_override", re.compile(p, re.I))
    for p in (
        r"\b(?:ignora|ignore|ignoren|olvida|olvide|omite)\s+(?:todas?\s+)?(?:las\s+|los\s+|tus\s+)?"
        r"(?:instrucciones|indicaciones|ordenes|reglas)\s+(?:anteriores|previas)",
        r"\b(?:ignore[zr]?|oublie[zr]?)\s+(?:toutes\s+)?(?:les\s+|tes\s+|vos\s+)?"
        r"(?:instructions|consignes|regles)\s+(?:precedentes|anterieures)",
        r"\b(?:ignoriere|ignorier|ignorieren\s+sie|vergiss|vergessen\s+sie)\s+(?:alle\s+)?"
        r"(?:deine\s+|die\s+)?(?:vorherigen|bisherigen|vorigen|obigen|fruheren)\s+"
        r"(?:anweisungen|instruktionen|befehle|regeln)",
        r"\b(?:ignore|ignora|esqueca|esquece)\s+(?:todas\s+)?(?:as\s+)?(?:instrucoes|ordens|regras)\s+"
        r"(?:anteriores|previas)",
        r"\b(?:ignora|ignorate|dimentica)\s+(?:tutte\s+)?(?:le\s+)?(?:istruzioni|regole)\s+"
        r"(?:precedenti|anteriori)",
        r"\b(?:onceki|yukaridaki)\s+(?:tum\s+|butun\s+)?(?:talimatlari|komutlari)\s+"
        r"(?:yok\s+say|gormezden\s+gel|unut)",
        r"(?:игнорируй|игнорируйте|проигнорируй|забудь|забудьте)\s+(?:все\s+)?"
        r"(?:предыдущие|прошлые|прежние|вышеуказанные)\s+(?:инструкции|указания|правила)",
        r"(?:忽略|无视|忽视|忘记|忘掉)(?:掉)?(?:之前|以前|上面|先前|前面|所有)(?:的)?(?:所有)?(?:的)?"
        r"(?:指令|指示|说明|规则|提示)",
        r"(?:以前|前|上記)の(?:すべての|全ての)?(?:指示|命令|ルール)を(?:すべて|全て)?(?:無視|忘れ)",
        r"(?:تجاهل|انس|تجاهلي)\s+(?:جميع\s+|كل\s+)?(?:التعليمات|الأوامر|التوجيهات)\s+السابقة",
        r"(?:پچھلی|سابقہ|گزشتہ)\s+(?:تمام\s+|سب\s+)?(?:ہدایات|ہدایتوں|احکامات)\s+(?:کو\s+)?"
        r"(?:نظر\s*انداز|بھول)",
        r"(?:पिछले|पिछली|पूर्व)\s+(?:सभी\s+)?(?:निर्देशों|निर्देश)\s+(?:को\s+)?(?:अनदेखा|भूल)",
    )
] + [
    ("system_prompt_exfil", re.compile(p, re.I))
    for p in (
        r"\b(?:muestra|revela|dime|imprime|muestrame)\s+(?:el\s+|tu\s+)?(?:prompt|mensaje)\s+"
        r"del\s+sistema",
        r"\b(?:montre|revele|affiche)[sz]?\s+(?:moi\s+)?(?:le\s+|ton\s+|votre\s+)?prompt\s+"
        r"(?:du\s+)?systeme",
        r"(?:显示|透露|输出|告诉我|泄露)(?:你的)?系统提示",
    )
]
INJECTION_PATTERNS = [*_VENDOR_INJECTION, *_INJ_EXTRA, *_INJ_LANG]

# phrases checked on the compact form (letters and digits only), for "i g n o r e" style spacing
_COMPACT = re.compile(
    r"(?:ignore|disregard|forget|override)(?:all|any|the|your|every|my)*"
    r"(?:previous|prior|above|earlier|preceding|system)(?:instructions?|prompts?|rules|directives?)"
    r"|(?:reveal|print|show|repeat|leak)(?:your|the)(?:system)?(?:prompt|instructions)"
    r"|youarenow(?:a|an|in)(?:dan|developer|god|jailbreak)"
)

# ----------------------------------------------------------------------------- canonical forms

# Cyrillic and Greek letters that render like Latin ones (text is lower-cased first)
_HOMOGLYPHS_RAW = {
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
_HOMOGLYPHS = str.maketrans(_HOMOGLYPHS_RAW)


# for secret detection: the same look-alikes in both cases, plus every dash, mapped one character
# for one, so a match in this view is a match at the same offsets in the text
_SECRET_FOLD = str.maketrans(
    {
        **_HOMOGLYPHS_RAW,
        **{k.upper(): v.upper() for k, v in _HOMOGLYPHS_RAW.items() if len(k.upper()) == 1},
        **dict.fromkeys("‐‑‒–—―−﹣", "-"),
    }
)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s"}
)
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


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


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)


def _hits(text: str, folded: str | None = None) -> bool:
    f = fold(text) if folded is None else folded
    return any(p.search(v) for v in (text, f) for _, p in INJECTION_PATTERNS) or bool(
        _COMPACT.search(re.sub(r"[^a-z0-9]+", "", f))
    )


def injection_findings(text: str) -> list[str]:
    """Names of the injection patterns the text matches, by any of its canonical forms."""
    c, f = canon(text), fold(text)
    found: list[str] = []
    for v in {text, c, f}:
        for label, pat in INJECTION_PATTERNS:
            if pat.search(v):
                found.append(f"injection:{label}")
    if _COMPACT.search(re.sub(r"[^a-z0-9]+", "", f)):
        found.append("injection:instruction_override")
    # leetspeak ("1gn0r3 pr3v10us"): digits and symbols read as the letters they stand for
    if _COMPACT.search(re.sub(r"[^a-z0-9]+", "", f.translate(_LEET))):
        found.append("injection:obfuscated")
    # written so that a filter reading the text does not see it: HTML entities, backslash
    # escapes, ROT13 (of the folded text, which is already lower case and accent-free)
    if "&" in text and _hits(html.unescape(text)):
        found.append("injection:encoded")
    if "\\" in text and _hits(_unescape(text)):
        found.append("injection:encoded")
    rot = codecs.decode(f, "rot13")
    if _hits(rot, rot):
        found.append("injection:encoded")
    for tok in _TOKEN.findall(c):
        for dec in _decodings(tok):
            if _hits(dec):
                found.append("injection:encoded")
    return sorted(set(found))


def _redact(text: str, patterns, findings: list[str]) -> str:
    """Patterns are matched on the look-alike-folded view (a Cyrillic "ѕ" or an en dash in a key
    still makes it a key) and the same spans are replaced in the text itself, so text that only
    contains such letters is never rewritten."""
    for label, pat in patterns:
        spans = [m.span() for m in pat.finditer(text.translate(_SECRET_FOLD))]
        if not spans:
            continue
        findings.append(f"redacted:{label}")
        out, last = [], 0
        for a, b in spans:
            out += [text[last:a], f"[REDACTED_{label.upper()}]"]
            last = b
        text = "".join(out) + text[last:]
    return text


# characters written one at a time with a separator: "s k - a b c ...", "s.k.-.a.b.c..."
_SPACED = re.compile(r"(?<!\S)(?:\S(?:[ \t]{1,2}|[ \t]?[.·][ \t]?)){10,400}\S(?!\S)")


def _redact_spaced(text: str, patterns, findings: list[str]) -> str:
    def sub(m: re.Match) -> str:
        joined = re.sub(r"[ \t.·]", "", m.group(0)).translate(_SECRET_FOLD)
        hit = _scan(joined, patterns)
        if hit:
            findings.append(f"redacted:{hit[0]}")
            return f"[REDACTED_{hit[0].upper()}]"
        return m.group(0)

    return _SPACED.sub(sub, text)


_PCT = re.compile(r"(?<![^\s%])[^\s%]{0,200}+(?:%[0-9A-Fa-f]{2}[^\s%]{0,200}+){1,60}")


def _redact_encoded(text: str, patterns, findings: list[str]) -> str:
    def sub(m: re.Match) -> str:
        for dec in _decodings(m.group(0)):
            hit = _scan(dec, patterns)
            if hit:
                findings.append(f"redacted:encoded_{hit[0]}")
                return f"[REDACTED_ENCODED_{hit[0].upper()}]"
        return m.group(0)

    def pct(m: re.Match) -> str:
        hit = _scan(unquote(m.group(0)), patterns)
        if hit:
            findings.append(f"redacted:encoded_{hit[0]}")
            return f"[REDACTED_ENCODED_{hit[0].upper()}]"
        return m.group(0)

    return _TOKEN.sub(sub, _PCT.sub(pct, text) if "%" in text else text)


def _squash(text: str) -> str:
    """Collapse any run of whitespace to one space so ``project   orion`` still matches."""
    return " ".join(text.split())


def _term_pattern(terms) -> re.Pattern | None:
    parts = [r"\s+".join(re.escape(w) for w in t.split()) for t in terms if t and t.strip()]
    return re.compile("|".join(parts), re.I) if parts else None


def term_findings(text: str, deny_terms) -> list[str]:
    """``policy:deny_term`` when any of a tenant's literal terms appears (any case, any look-alike)."""
    if not deny_terms:
        return []
    f = _squash(fold(text))
    return (
        ["policy:deny_term"]
        if any(_squash(fold(t)) and _squash(fold(t)) in f for t in deny_terms)
        else []
    )


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
    pat = _term_pattern(redact_terms) if redact_terms else None
    if pat:
        if pat.search(view):
            findings.append("redacted:custom_term")
            view = pat.sub("[REDACTED_TERM]", view)
    patterns = SECRET_PATTERNS + (PII_PATTERNS if redact_pii else [])
    red = _redact(view, SECRET_PATTERNS, findings)
    if redact_pii:
        red = _redact(red, PII_PATTERNS, findings)
    red = _redact_encoded(red, patterns, findings)
    red = _redact_spaced(red, patterns, findings)
    if not findings:
        return GuardResult(allowed=True, text=text, findings=[])
    return GuardResult(allowed=True, text=red, findings=sorted(set(findings)))


def screen_output(text: str, *, redact_pii: bool = False, redact_terms=()) -> GuardResult:
    """Applied to what the model said: it may echo a secret it saw elsewhere."""
    return screen(text, redact_pii=redact_pii, check_injection=False, redact_terms=redact_terms)


def screen_joined(texts: list[str], *, redact_pii: bool = False) -> tuple[list[str], list[str]]:
    """Redact a secret (or, with ``redact_pii``, an email address) that only exists once the
    messages are read together: half a key in one message and the rest in the next. Each message
    has already been screened on its own; only matches that cross a message boundary are new.
    A message such a match touches comes back canonicalised, with its part of the match replaced."""
    patterns = SECRET_PATTERNS + ([p for p in PII_PATTERNS if p[0] == "email"] if redact_pii else [])
    segs = [canon(t) for t in texts]
    joined = "".join(segs).translate(_SECRET_FOLD)
    bounds, at = [], 0
    for sg in segs:
        bounds.append((at, at + len(sg)))
        at += len(sg)
    cuts: dict[int, list[tuple[int, int, str]]] = {}
    findings: list[str] = []
    for label, pat in patterns:
        for m in pat.finditer(joined):
            touched = [i for i, (a, b) in enumerate(bounds) if a < m.end() and m.start() < b]
            if len(touched) < 2:
                continue
            findings.append(f"redacted:{label}")
            for i in touched:
                a, b = bounds[i]
                cuts.setdefault(i, []).append(
                    (max(m.start(), a) - a, min(m.end(), b) - a, f"[REDACTED_{label.upper()}]")
                )
    if not cuts:
        return list(texts), []
    out = list(texts)
    for i, spans in cuts.items():
        t, last, parts = segs[i], 0, []
        for a, b, ph in sorted(spans):
            if a < last:
                continue
            parts += [t[last:a], ph]
            last = b
        out[i] = "".join(parts) + t[last:]
    return out, sorted(set(findings))


def redact_label(text: str) -> str:
    """A short label (a feature name, a requested model) with keys and addresses removed."""
    return screen(text, redact_pii=True, check_injection=False).text
