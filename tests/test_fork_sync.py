"""Fork healing when a block arrives before its parent (B-10).

Two real nodes (``create_app``) talk through Flask test clients instead of sockets:
``requests`` inside the node and synchronizer modules is swapped for an in-memory
transport, and gossip threads run inline, so the test is deterministic.
"""

import threading
import types

import pytest
import requests

import network.node as node_module
import sync.synchronizer as sync_module
from network.node import create_app

DIFFICULTY = 1


class _Response:
    def __init__(self, flask_response) -> None:
        self.status_code = flask_response.status_code
        self._json = flask_response.get_json(silent=True)

    def json(self):
        return self._json


class _Transport:
    """Stands in for the ``requests`` module: routes http://<name>/... to a test client."""

    RequestException = requests.RequestException

    def __init__(self) -> None:
        self.clients: dict = {}

    def _route(self, url: str):
        host, _, path = url.removeprefix("http://").partition("/")
        if host not in self.clients:
            raise requests.ConnectionError(f"no such node: {host}")
        return self.clients[host], "/" + path

    def get(self, url: str, **_kwargs) -> _Response:
        client, path = self._route(url)
        return _Response(client.get(path))

    def post(self, url: str, json=None, **_kwargs) -> _Response:
        client, path = self._route(url)
        return _Response(client.post(path, json=json))


class _InlineThread:
    """Runs a gossip send immediately on start(), so delivery order is fixed."""

    def __init__(self, target, args=(), daemon=None) -> None:
        self._target, self._args = target, args

    def start(self) -> None:
        self._target(*self._args)


def _cfg(port: int) -> types.ModuleType:
    cfg = types.ModuleType("cfg")
    cfg.DIFFICULTY = DIFFICULTY  # type: ignore[attr-defined]
    cfg.SEED_PEER = ""  # type: ignore[attr-defined]
    cfg.HOST_ADDR = "127.0.0.1"  # type: ignore[attr-defined]
    cfg.PORT = port  # type: ignore[attr-defined]
    cfg.MINER_ADDRESS = f"miner_{port}"  # type: ignore[attr-defined]
    cfg.MINING_REWARD = 10.0  # type: ignore[attr-defined]
    return cfg


@pytest.fixture
def net(monkeypatch):
    transport = _Transport()
    monkeypatch.setattr(node_module, "requests", transport)
    monkeypatch.setattr(sync_module, "requests", transport)
    monkeypatch.setattr(
        node_module, "threading", types.SimpleNamespace(Thread=_InlineThread, Lock=threading.Lock)
    )
    return transport


def _tip(client) -> str:
    return client.get("/chain").get_json()["chain"][-1]["hash"]


def _mine(client) -> dict:
    resp = client.post("/mine")
    assert resp.status_code == 201
    return resp.get_json()["block"]


@pytest.mark.xfail(
    strict=True, reason="B-11: catch-up fetches by index, so a missing fork parent is never fetched"
)
def test_node_on_fork_catches_up_when_child_arrives_before_parent(net):
    """A node on a losing fork receives the winning chain's newest block before its parent.

    Gossip sends each block in its own thread (network/node.py:64), so a child can arrive
    first; add_block rejects it for an unknown parent (blockchain/chain.py:61). The node then
    catches up with sync_incremental (network/node.py:152), which asks peers for blocks with
    index > its own tip index (sync/synchronizer.py:122, network/node.py:191). Its own fork
    block already fills the parent's index, so the peer returns only the child again, the
    missing parent is never requested, and the node stays on the shorter fork.
    """
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})

    # Common history: genesis <- p, known to both nodes.
    p = _mine(a)
    assert b.post("/blocks/new", json=p).status_code == 201

    # Fork at height 2: B mines b1; A mines a1 then a2 (A's branch is longer).
    _mine(b)
    a1 = _mine(a)
    a2 = _mine(a)
    assert a2["previous_hash"] == a1["hash"]

    # B learns about A, then a2 arrives before a1 (out-of-order gossip).
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200
    b.post("/blocks/new", json=a2)

    # B had every chance to catch up from A, so it must now be on A's longer chain.
    assert _tip(b) == _tip(a) == a2["hash"]
