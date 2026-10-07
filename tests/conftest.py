import pytest
from fastapi.testclient import TestClient

from llm_control_room.app import create_app
from llm_control_room.core import Core
from llm_control_room.gateway import GatewayRequest
from llm_control_room.store import Store


@pytest.fixture()
def core():
    c = Core(Store(":memory:"))
    c.seed_tenants()
    return c


@pytest.fixture()
def app():
    return create_app(":memory:")


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def sim(app):
    return app.state.sim


def ask(core, prompt, tenant="acme", **kw):
    return core.gateway.handle(
        GatewayRequest(tenant=tenant, messages=[{"role": "user", "content": prompt}], **kw)
    )
