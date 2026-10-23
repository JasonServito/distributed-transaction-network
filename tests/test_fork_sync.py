"""Fork healing and catch-up between nodes.

Real nodes (``create_app``) talk through Flask test clients instead of sockets:
``requests`` inside the node and synchronizer modules is swapped for an in-memory
transport, and gossip threads run inline, so the tests are deterministic.
"""

import threading
import types

import pytest
import requests
from flask import Flask

import network.node as node_module
import sync.synchronizer as sync_module
from blockchain.block import Block
from blockchain.chain import Blockchain
from blockchain.transaction import Transaction
from consensus.proof_of_work import ProofOfWork
from network.node import create_app
from sync.synchronizer import Synchronizer

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


def _block_on(parent: Block) -> Block:
    """Mine a block on *parent* (its coinbase tx_id keeps sibling blocks distinct)."""
    block = Block(
        index=parent.index + 1,
        previous_hash=parent.hash,
        transactions=[Transaction(sender="COINBASE", receiver="miner", amount=10.0)],
    )
    return ProofOfWork(DIFFICULTY).mine(block)


def _peer_serving(blocks: list):
    """A stand-in peer that only serves GET /chain, with *blocks* as its main chain."""
    peer = Flask("peer")
    peer.add_url_rule("/chain", view_func=lambda: {"chain": [b.to_dict() for b in blocks]})
    return peer.test_client()


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


# ------------------------------------------------------------------ #
#  Full sync goes through add_block (B-12)
# ------------------------------------------------------------------ #


def test_full_sync_switches_to_a_longer_chain_and_keeps_side_branches(net):
    ours = Blockchain()
    genesis = ours.last_block
    p = _block_on(genesis)
    b1, a1 = _block_on(p), _block_on(p)
    for block in (p, b1, a1):
        assert ours.add_block(block, DIFFICULTY)
    assert ours.last_block.hash == b1.hash  # a1 is a side branch: same height, seen second

    c1 = _block_on(p)
    c2 = _block_on(c1)
    net.clients["peer"] = _peer_serving([genesis, p, c1, c2])

    assert Synchronizer(ours, DIFFICULTY).sync_with_peers({"peer"}) is True
    assert ours.last_block.hash == c2.hash
    assert ours.has_block(b1.hash) and ours.has_block(a1.hash)
    assert ours.known_block_count() == 6


def test_full_sync_stops_at_the_first_invalid_block(net):
    ours = Blockchain()
    genesis = ours.last_block
    c1 = _block_on(genesis)
    c2 = _block_on(c1)
    c2.nonce += 1  # its hash no longer matches its contents: bad proof-of-work
    c3 = _block_on(c2)
    net.clients["peer"] = _peer_serving([genesis, c1, c2, c3])

    assert Synchronizer(ours, DIFFICULTY).sync_with_peers({"peer"}) is True
    assert ours.last_block.hash == c1.hash
    assert not ours.has_block(c2.hash) and not ours.has_block(c3.hash)


def test_full_sync_from_a_shorter_chain_keeps_ours_and_stores_theirs(net):
    ours = Blockchain()
    genesis = ours.last_block
    p = _block_on(genesis)
    b1 = _block_on(p)
    for block in (p, b1):
        assert ours.add_block(block, DIFFICULTY)

    c1 = _block_on(genesis)
    net.clients["peer"] = _peer_serving([genesis, c1])

    assert Synchronizer(ours, DIFFICULTY).sync_with_peers({"peer"}) is False
    assert ours.last_block.hash == b1.hash
    assert ours.has_block(c1.hash)  # kept as a side branch, like any other valid block
