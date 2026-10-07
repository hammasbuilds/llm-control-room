# Vendored copy. Source: llm-observability-platform/src/llmobs/drift/signature.py (commit 790d19d), MIT licence, (c) 2026 Muhammad Hammas. Unmodified.
"""Prompt fingerprints.

The prompt itself is never stored. A support ticket or a medical question is not
something a monitoring system should retain, and "we only keep it for debugging" is
how data ends up somewhere it should not be.

What is stored is the *shape*: length bucket, question type, and a hash of the
structural skeleton with all content words removed. That is enough to detect the
distribution moving and useless to anyone who obtains it.
"""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[a-z']+")

# Function words survive; content words are replaced. Two prompts with the same
# skeleton are asking the same *kind* of question about different things.
_SKELETON_KEEP = {
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "which",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "will",
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "for",
    "to",
    "from",
    "with",
    "and",
    "or",
    "not",
    "no",
    "if",
    "this",
    "that",
    "these",
    "those",
    "i",
    "you",
    "we",
    "they",
    "it",
    "me",
    "my",
}

_QUESTION_TYPES = [
    ("factual", re.compile(r"^\s*(what|who|when|where|which)\b", re.I)),
    ("causal", re.compile(r"^\s*why\b", re.I)),
    ("procedural", re.compile(r"^\s*how\b", re.I)),
    ("yes_no", re.compile(r"^\s*(is|are|was|were|do|does|did|can|could|should|will)\b", re.I)),
    ("imperative", re.compile(r"^\s*(write|make|create|generate|summari[sz]e|translate)\b", re.I)),
]


def question_type(prompt: str) -> str:
    for label, pattern in _QUESTION_TYPES:
        if pattern.search(prompt):
            return label
    return "other"


def length_bucket(prompt: str) -> str:
    words = len(prompt.split())
    for limit, label in ((10, "xs"), (30, "s"), (100, "m"), (300, "l")):
        if words < limit:
            return label
    return "xl"


def skeleton_hash(prompt: str) -> str:
    """Hash of the prompt with content words replaced by a placeholder."""
    tokens = _WORD.findall(prompt.lower())
    skeleton = " ".join(t if t in _SKELETON_KEEP else "*" for t in tokens[:40])
    return hashlib.sha256(skeleton.encode()).hexdigest()[:12]


def signature(prompt: str) -> str:
    """A compact, non-reversible descriptor of prompt shape."""
    return f"{question_type(prompt)}:{length_bucket(prompt)}:{skeleton_hash(prompt)}"
