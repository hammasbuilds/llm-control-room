"""Parsers for content people upload or paste: prompt batches, tenant configs, term lists.

Every parser takes the text and an optional file name, returns what it understood plus a list of
line-level problems (so one bad line does not throw away a whole file), and never touches the
store. The HTTP layer applies the result.
"""

from __future__ import annotations

import csv
import io
import json

MAX_FILE_CHARS = 500_000  # upload limit shown in the UI (the request body cap is 1 MB)
MAX_PROMPTS = 500
MAX_PROMPT_CHARS = 20_000
MAX_TENANTS = 50
PROMPT_KEYS = ("prompt", "content", "text", "message", "input", "question")
TENANT_LIST_FIELDS = ("allowed_models", "fallbacks", "deny_terms", "redact_terms")
TENANT_FIELDS = (
    "budget_usd",
    "budget_window_s",
    "rpm",
    "min_quality",
    "redact_pii",
    "cache_enabled",
    *TENANT_LIST_FIELDS,
)


class InputError(ValueError):
    pass


def _clean(text: str) -> str:
    if not isinstance(text, str):
        raise InputError("content must be text")
    if len(text) > MAX_FILE_CHARS:
        raise InputError(f"the file is over {MAX_FILE_CHARS:,} characters")
    if "\x00" in text:
        raise InputError("the file contains NUL bytes: it is binary, not text")
    return text.lstrip("﻿")


def _ext(filename: str) -> str:
    return filename.lower().rsplit(".", 1)[-1] if "." in filename else ""


def _prompt_from_obj(obj, where: str, errors: list[str]) -> dict | None:
    if isinstance(obj, str):
        obj = {"prompt": obj}
    if not isinstance(obj, dict):
        errors.append(f"{where}: expected a string or an object, got {type(obj).__name__}")
        return None
    key = next((k for k in PROMPT_KEYS if isinstance(obj.get(k), str)), None)
    if key is None:
        errors.append(f"{where}: no text field (use one of: {', '.join(PROMPT_KEYS)})")
        return None
    out = {"prompt": obj[key]}
    for extra in ("system", "context"):
        if isinstance(obj.get(extra), str) and obj[extra].strip():
            out[extra] = obj[extra]
    return out


def parse_prompts(text: str, filename: str = "") -> dict:
    """JSONL (one string or object per line), a JSON array, CSV with a ``prompt`` column, or plain
    text with one prompt per line."""
    text = _clean(text)
    ext, body = _ext(filename), text.strip()
    if not body:
        raise InputError("the file is empty")
    errors: list[str] = []
    items: list[dict] = []
    if ext == "csv" or (not ext and "," in body.split("\n", 1)[0] and not body.startswith(("{", "[", '"'))):
        fmt = "csv"
        rows = list(csv.reader(io.StringIO(text)))
        rows = [r for r in rows if any(c.strip() for c in r)]
        head = [c.strip().lower() for c in rows[0]] if rows else []
        col = next((head.index(k) for k in PROMPT_KEYS if k in head), None)
        if col is not None:
            idx = {k: head.index(k) for k in ("system", "context") if k in head}
            for n, r in enumerate(rows[1:], start=2):
                if col >= len(r) or not r[col].strip():
                    errors.append(f"row {n}: empty prompt")
                    continue
                item = {"prompt": r[col]}
                for k, i in idx.items():
                    if i < len(r) and r[i].strip():
                        item[k] = r[i]
                items.append(item)
        elif rows and all(len(r) == 1 for r in rows):
            for r in rows:
                items.append({"prompt": r[0]})
        else:
            raise InputError(
                "the CSV has several columns but none is called "
                f"{' / '.join(PROMPT_KEYS[:3])}: add a header row with a 'prompt' column"
            )
    elif ext in ("jsonl", "ndjson", "json") or body.startswith(("{", "[", '"')):
        fmt = "jsonl"
        if body.startswith("[") and ext != "jsonl":
            try:
                arr = json.loads(body)
            except json.JSONDecodeError as e:
                raise InputError(f"not valid JSON: {e}") from e
            if not isinstance(arr, list):
                raise InputError("expected a JSON array")
            for n, o in enumerate(arr, start=1):
                p = _prompt_from_obj(o, f"item {n}", errors)
                if p:
                    items.append(p)
        else:
            for n, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError as e:
                    errors.append(f"line {n}: not valid JSON ({e.msg})")
                    continue
                p = _prompt_from_obj(o, f"line {n}", errors)
                if p:
                    items.append(p)
    else:
        fmt = "lines"
        items = [{"prompt": ln.strip()} for ln in text.splitlines() if ln.strip()]
    kept = []
    for i, it in enumerate(items, start=1):
        if not it["prompt"].strip():
            errors.append(f"prompt {i}: empty")
        elif len(it["prompt"]) > MAX_PROMPT_CHARS:
            errors.append(f"prompt {i}: over {MAX_PROMPT_CHARS:,} characters")
        else:
            kept.append(it)
    truncated = len(kept) > MAX_PROMPTS
    if truncated:
        errors.append(f"only the first {MAX_PROMPTS} prompts are used ({len(kept)} found)")
        kept = kept[:MAX_PROMPTS]
    if not kept:
        raise InputError("no usable prompts found" + (f" ({errors[0]})" if errors else ""))
    return {"format": fmt, "prompts": kept, "errors": errors[:20], "error_count": len(errors)}


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    raise InputError(f"{v!r} is not true/false")


def _split_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace("|", ";").split(";") if x.strip()]


def _tenant_policy(raw: dict) -> dict:
    out: dict = {}
    for k, v in raw.items():
        k = str(k).strip()
        if k not in TENANT_FIELDS:
            continue
        if isinstance(v, str) and not v.strip() and k not in TENANT_LIST_FIELDS:
            continue  # a blank CSV cell means "leave the default"
        if k in TENANT_LIST_FIELDS:
            out[k] = _split_list(v)
        elif k in ("redact_pii", "cache_enabled"):
            out[k] = _bool(v)
        elif k == "rpm":
            out[k] = int(float(v))
        else:
            out[k] = float(v)
    return out


def parse_tenants(text: str, filename: str = "") -> dict:
    """JSON (``{"tenants": [...]}``, a list, or ``{name: policy}``) or CSV with a ``name`` column.
    List fields in CSV are separated by ``;``. Unknown columns are reported, not silently used."""
    text = _clean(text)
    body = text.strip()
    if not body:
        raise InputError("the file is empty")
    errors: list[str] = []
    rows: list[dict] = []
    if _ext(filename) == "csv" or (not body.startswith(("{", "[")) and "," in body.split("\n")[0]):
        fmt = "csv"
        recs = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        head = [c.strip() for c in recs[0]] if recs else []
        if "name" not in head:
            raise InputError("the CSV needs a header row with a 'name' column")
        unknown = [h for h in head if h not in TENANT_FIELDS and h != "name"]
        if unknown:
            errors.append("ignored columns: " + ", ".join(unknown))
        for n, r in enumerate(recs[1:], start=2):
            rows.append({"_where": f"row {n}", **dict(zip(head, r, strict=False))})
    else:
        fmt = "json"
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise InputError(f"not valid JSON: {e}") from e
        if isinstance(data, dict) and isinstance(data.get("tenants"), list):
            data = data["tenants"]
        if isinstance(data, dict):
            if "name" in data:
                data = [data]
            else:
                data = [{"name": k, **v} if isinstance(v, dict) else {"name": k} for k, v in data.items()]
        if not isinstance(data, list):
            raise InputError('expected {"tenants": [...]}, a list, or an object keyed by name')
        for n, o in enumerate(data, start=1):
            if not isinstance(o, dict):
                errors.append(f"tenant {n}: expected an object")
                continue
            rows.append({"_where": f"tenant {n}", **o})
    tenants, seen = [], set()
    for r in rows:
        where = r.pop("_where")
        name = str(r.pop("name", "")).strip()
        unknown = [k for k in r if k not in TENANT_FIELDS and fmt == "json"]
        if unknown:
            errors.append(f"{where} ({name or '?'}): ignored fields " + ", ".join(map(str, unknown)))
        try:
            policy = _tenant_policy(r)
        except (InputError, ValueError, TypeError) as e:
            errors.append(f"{where} ({name or '?'}): {e}")
            continue
        if name in seen:
            errors.append(f"{where}: {name!r} appears twice, the later one is skipped")
            continue
        seen.add(name)
        tenants.append({"name": name, "policy": policy})
    if len(tenants) > MAX_TENANTS:
        raise InputError(f"at most {MAX_TENANTS} tenants per file")
    if not tenants:
        raise InputError("no tenants found")
    return {"format": fmt, "tenants": tenants, "errors": errors[:20], "error_count": len(errors)}


def parse_terms(text: str, filename: str = "") -> dict:
    """One term per line (a ``#`` line is a comment), a JSON array of strings, or a CSV whose first
    column is the terms. Terms are literal text, never patterns."""
    text = _clean(text)
    body = text.strip()
    if not body:
        raise InputError("the file is empty")
    errors: list[str] = []
    ext = _ext(filename)
    if body.startswith("["):
        try:
            arr = json.loads(body)
        except json.JSONDecodeError as e:
            raise InputError(f"not valid JSON: {e}") from e
        if not isinstance(arr, list) or not all(isinstance(x, str) for x in arr):
            raise InputError("a JSON term list must be an array of strings")
        raw = arr
    elif ext == "csv":
        raw = [r[0] for r in csv.reader(io.StringIO(text)) if r and r[0].strip()]
        if raw and raw[0].strip().lower() in ("term", "terms", "word", "words"):
            raw = raw[1:]
    else:
        raw = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    terms, seen = [], set()
    for t in raw:
        t = t.strip()
        if not t:
            continue
        if len(t) > 200:
            errors.append(f"skipped a term over 200 characters ({t[:20]!r}...)")
            continue
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        terms.append(t)
    if not terms:
        raise InputError("no terms found")
    return {"terms": terms, "errors": errors[:20], "error_count": len(errors)}
