"""Wires the pieces together and seeds the demo tenants."""

from __future__ import annotations

import time

from .agents import AgentRunner
from .gateway import Gateway
from .obs import Alerts
from .providers import ProviderSet
from .releases import Releases
from .store import Store
from .tenants import Tenants

DEMO_TENANTS = {
    "acme": {"budget_usd": 25.0, "rpm": 600, "redact_pii": True, "min_quality": 0.75},
    "globex": {"budget_usd": 25.0, "rpm": 600, "redact_pii": True, "min_quality": 0.85},
    "initech": {"budget_usd": 25.0, "rpm": 600, "redact_pii": False, "min_quality": 0.75},
}


def demo_key(tenant: str) -> str:
    return f"lcr-demo-{tenant}"


class Core:
    def __init__(self, store: Store, providers: ProviderSet | None = None) -> None:
        self.store = store
        self.providers = providers or ProviderSet()
        self.real_clock = time.time
        self.clock = time.time
        self.tenants = Tenants(store, lambda: self.clock())
        self.releases = Releases(store, lambda: self.clock())
        self.gateway = Gateway(
            store, self.providers, self.tenants, self.releases, lambda: self.clock()
        )
        self.alerts = Alerts(store, lambda: self.clock())
        self.runner = AgentRunner(store, self.gateway)
        self.calls_seen = 0
        self.gateway.on_call.append(self._tick)

    def _tick(self) -> None:
        self.calls_seen += 1
        if self.calls_seen % 50 == 0:
            self.alerts.evaluate()

    def set_clock(self, fn=None) -> None:
        self.clock = fn or self.real_clock

    def seed_tenants(self) -> None:
        for name, policy in DEMO_TENANTS.items():
            if not self.tenants.get(name):
                self.tenants.create(name, **policy)
            if not any(not k["revoked"] for k in self.tenants.keys(name)):
                self.tenants.add_key(name, "demo key", key=demo_key(name))
