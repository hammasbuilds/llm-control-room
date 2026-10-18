"""SQLite storage. One writing connection behind one lock, WAL, plus per-thread read connections
for the long admin reads. No prompt text is ever written here."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    name TEXT PRIMARY KEY, created REAL NOT NULL,
    budget_usd REAL NOT NULL DEFAULT 5.0, budget_window_s REAL NOT NULL DEFAULT 86400,
    rpm INTEGER NOT NULL DEFAULT 600, allowed_models TEXT NOT NULL DEFAULT '[]',
    redact_pii INTEGER NOT NULL DEFAULT 1, cache_enabled INTEGER NOT NULL DEFAULT 1,
    min_quality REAL NOT NULL DEFAULT 0.75, fallbacks TEXT NOT NULL DEFAULT '[]',
    deny_terms TEXT NOT NULL DEFAULT '[]', redact_terms TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trace TEXT NOT NULL, at REAL NOT NULL,
    tenant TEXT NOT NULL, feature TEXT NOT NULL DEFAULT 'unknown',
    release TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 0,
    requested TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '', route_reason TEXT NOT NULL DEFAULT '',
    diff_est INTEGER NOT NULL DEFAULT 1, diff_true INTEGER,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0, baseline_usd REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0, attempts TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '', error_kind TEXT NOT NULL DEFAULT '',
    redactions TEXT NOT NULL DEFAULT '[]', grounding REAL, quality_ok INTEGER,
    prompt_len INTEGER NOT NULL DEFAULT 0, signature TEXT NOT NULL DEFAULT '',
    shadow INTEGER NOT NULL DEFAULT 0, shadow_sim REAL, source TEXT NOT NULL DEFAULT 'api');
-- (tenant, at, usd) covers the gateway's budget check, a sum over the tenant's whole budget
-- window on every request; it replaces the (tenant, at) index of earlier versions
CREATE INDEX IF NOT EXISTS calls_tenant_at_usd ON calls(tenant, at, usd);
DROP INDEX IF EXISTS calls_tenant_at;
CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
CREATE INDEX IF NOT EXISTS calls_release ON calls(release, version, at);
-- the admin pages read the served and the shadow calls of a window in call order
CREATE INDEX IF NOT EXISTS calls_shadow_at ON calls(shadow, at);
CREATE TABLE IF NOT EXISTS releases (
    name TEXT PRIMARY KEY, created REAL NOT NULL, challenger_mode TEXT NOT NULL DEFAULT 'canary',
    auto_rollback INTEGER NOT NULL DEFAULT 1, slo TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS versions (
    release TEXT NOT NULL, version INTEGER NOT NULL, model TEXT NOT NULL DEFAULT 'auto',
    system_prompt TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'archived', traffic REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
    PRIMARY KEY (release, version));
CREATE TABLE IF NOT EXISTS release_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, release TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, rule TEXT NOT NULL,
    severity TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
    value REAL, threshold REAL);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created REAL NOT NULL, tenant TEXT NOT NULL, scenario TEXT NOT NULL,
    goal TEXT NOT NULL, status TEXT NOT NULL, limits TEXT NOT NULL DEFAULT '{}',
    profile TEXT NOT NULL DEFAULT 'restricted', result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


READERS = 3  # read connections per store: admin pages computed at the same time


def default_home() -> Path:
    return Path(os.environ.get("LCR_HOME") or Path.home() / ".llm-control-room")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        with self.lock:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
            self.con.executescript(SCHEMA)
            # databases made by an earlier version lack the newer tenant columns
            have = {r["name"] for r in self.con.execute("PRAGMA table_info(tenants)")}
            for col in ("deny_terms", "redact_terms"):
                if col not in have:
                    self.con.execute(
                        f"ALTER TABLE tenants ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'"
                    )
        self._depth = 0
        self._readers: list[sqlite3.Connection] = []
        self._idle: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self._readers_lock = threading.Lock()  # never self.lock: readers must not wait on it

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group many writes into one commit. Re-entrant."""
        with self.lock:
            if self._depth == 0:
                self.con.execute("BEGIN")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.con.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.con.execute("COMMIT")

    def run(self, sql: str, args: tuple | list = ()) -> int:
        with self.lock:
            cur = self.con.execute(sql, args)
            return cur.lastrowid or 0

    def all(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """A connection for long reads (the admin pages), inside one read transaction so
        everything read through it is one snapshot. On a file database it is one of a few
        pooled read connections and never takes ``self.lock``: in WAL mode a reader does not
        block the writer, so the gateway goes on recording calls while a page is computed. A
        ``:memory:`` database exists only on the main connection, so there it is that one,
        under the lock."""
        if self.path == ":memory:":
            with self.lock:
                yield from self._snapshot(self.con)
            return
        con = self._take_reader()
        try:
            yield from self._snapshot(con)
        finally:
            self._idle.put(con)

    def _take_reader(self) -> sqlite3.Connection:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._readers_lock:
            make = len(self._readers) < READERS
            if make:
                con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                con.row_factory = sqlite3.Row
                # a window's GROUP BY sorts fit in memory, on two helper threads
                con.execute("PRAGMA cache_size=-32768")
                con.execute("PRAGMA temp_store=MEMORY")
                con.execute("PRAGMA threads=2")
                self._readers.append(con)
        return con if make else self._idle.get()

    @staticmethod
    def _snapshot(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        # a savepoint works both alone (it opens the transaction) and nested in transaction()
        con.execute("SAVEPOINT lcr_read")
        try:
            yield con
        finally:
            con.execute("ROLLBACK TO lcr_read")
            con.execute("RELEASE lcr_read")

    def kv_get(self, key: str, default=None):
        row = self.one("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(row["v"]) if row else default

    def kv_set(self, key: str, value) -> None:
        self.run(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )

    def wipe_traffic(self) -> None:
        with self.transaction():
            for t in ("calls", "alerts", "release_events", "run_events", "runs"):
                self.run(f"DELETE FROM {t}")

    def close(self) -> None:
        with self.lock, self._readers_lock:
            for con in self._readers:
                con.close()
            self._readers.clear()
            self.con.close()
