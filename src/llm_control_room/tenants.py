"""Tenants, their policies, and their API keys (stored as SHA-256 hashes)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

from .store import Store

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
FIELDS = (
    "budget_usd",
    "budget_window_s",
    "rpm",
    "allowed_models",
    "redact_pii",
    "cache_enabled",
    "min_quality",
    "fallbacks",
    "deny_terms",
    "redact_terms",
)


class TenantError(ValueError):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _row(r: dict) -> dict:
    out = dict(r)
    out["allowed_models"] = json.loads(r["allowed_models"])
    out["fallbacks"] = json.loads(r["fallbacks"])
    out["deny_terms"] = json.loads(r["deny_terms"])
    out["redact_terms"] = json.loads(r["redact_terms"])
    out["redact_pii"] = bool(r["redact_pii"])
    out["cache_enabled"] = bool(r["cache_enabled"])
    return out


def _validate(p: dict) -> dict:
    out = {}
    for k, v in p.items():
        if k not in FIELDS:
            continue
        if k in ("budget_usd", "budget_window_s", "min_quality"):
            v = float(v)
            if v < 0 or (k == "min_quality" and v > 1) or (k == "budget_window_s" and v <= 0):
                raise TenantError(f"{k} out of range")
        elif k == "rpm":
            v = int(v)
            if v < 1:
                raise TenantError("rpm must be at least 1")
        elif k in ("allowed_models", "fallbacks"):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise TenantError(f"{k} must be a list of model ids")
            v = json.dumps(v)
        elif k in ("deny_terms", "redact_terms"):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise TenantError(f"{k} must be a list of strings")
            v = [t.strip() for t in v if t.strip()]
            if len(v) > 100 or any(len(t) > 200 for t in v):
                raise TenantError(f"{k}: at most 100 terms of at most 200 characters")
            v = json.dumps(v)
        else:
            v = 1 if v else 0
        out[k] = v
    return out


class Tenants:
    def __init__(self, store: Store, clock=time.time) -> None:
        self.store = store
        self.clock = clock
        # called with the tenant name whenever its policy changes or it is deleted, so a cache
        # that was filled under the old policy (or for a tenant that no longer exists) is dropped
        self.on_change: list = []

    def _changed(self, name: str) -> None:
        for cb in self.on_change:
            cb(name)

    def check_policy(self, policy: dict) -> None:
        """Raise TenantError if the policy would be refused, without writing anything."""
        _validate(policy)

    def create(self, name: str, **policy) -> dict:
        if not NAME_RE.match(name or ""):
            raise TenantError("name must be lowercase letters, digits, - or _ (max 40)")
        if self.get(name):
            raise TenantError(f"tenant {name!r} already exists")
        vals = _validate(policy)
        cols = ["name", "created", *vals]
        self.store.run(
            f"INSERT INTO tenants({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [name, self.clock(), *vals.values()],
        )
        return self.get(name)

    def update(self, name: str, **policy) -> dict:
        if not self.get(name):
            raise TenantError(f"no tenant {name!r}")
        vals = _validate(policy)
        if vals:
            sets = ",".join(f"{k}=?" for k in vals)
            self.store.run(f"UPDATE tenants SET {sets} WHERE name=?", [*vals.values(), name])
            self._changed(name)
        return self.get(name)

    def delete(self, name: str) -> None:
        with self.store.transaction():
            self.store.run("DELETE FROM tenants WHERE name=?", (name,))
            self.store.run("DELETE FROM api_keys WHERE tenant=?", (name,))
            # keep the history for the record, but under a name nobody can register again, so a
            # tenant created later with the same name does not inherit this one's spend
            self.store.run("UPDATE calls SET tenant=? WHERE tenant=?", (f"{name}~deleted", name))
        self._changed(name)

    def get(self, name: str) -> dict | None:
        r = self.store.one("SELECT * FROM tenants WHERE name=?", (name,))
        return _row(r) if r else None

    def list(self) -> list[dict]:
        return [_row(r) for r in self.store.all("SELECT * FROM tenants ORDER BY name")]

    def add_key(self, tenant: str, label: str = "", *, key: str | None = None) -> dict:
        if not self.get(tenant):
            raise TenantError(f"no tenant {tenant!r}")
        key = key or "lcr-" + secrets.token_hex(16)
        kid = self.store.run(
            "INSERT INTO api_keys(tenant, key_hash, prefix, label, created) VALUES(?,?,?,?,?)",
            (tenant, hash_key(key), key[:10], label, self.clock()),
        )
        return {"id": kid, "tenant": tenant, "key": key, "prefix": key[:10], "label": label}

    def keys(self, tenant: str) -> list[dict]:
        return self.store.all(
            "SELECT id, tenant, prefix, label, created, revoked FROM api_keys WHERE tenant=? "
            "ORDER BY id",
            (tenant,),
        )

    def revoke(self, key_id: int) -> None:
        self.store.run("UPDATE api_keys SET revoked=1 WHERE id=?", (key_id,))

    def authenticate(self, key: str) -> str | None:
        r = self.store.one(
            "SELECT tenant FROM api_keys WHERE key_hash=? AND revoked=0", (hash_key(key or ""),)
        )
        return r["tenant"] if r else None
