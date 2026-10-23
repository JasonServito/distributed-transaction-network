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
from utils.signing import generate_keypair, public_key_to_pem

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
        self.gets: list = []  # paths requested with GET, in order

    def _route(self, url: str):
        host, _, path = url.removeprefix("http://").partition("/")
        if host not in self.clients:
            raise requests.ConnectionError(f"no such node: {host}")
        return self.clients[host], "/" + path

    def get(self, url: str, **_kwargs) -> _Response:
        client, path = self._route(url)
        self.gets.append(path)
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


def test_node_on_fork_catches_up_when_child_arrives_before_parent(net):
    """A node on a losing fork receives the winning chain's newest block before its parent.

    Gossip sends each block in its own thread, so a child can arrive first. The node must
    fetch the missing parent by hash and switch to the longer chain. Before B-11, catch-up
    only asked peers for blocks with an index above its own tip; its own fork block already
    filled the parent's index, so the parent was never requested and the node stayed on the
    shorter fork (B-10).
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


# ------------------------------------------------------------------ #
#  Missing ancestors are fetched by hash (B-11)
# ------------------------------------------------------------------ #


def test_get_block_by_hash_serves_main_chain_and_side_branch_blocks(net):
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    main = _mine(a)
    side = _mine(b)  # same height as `main`, so A keeps it as a side branch
    assert a.post("/blocks/new", json=side).status_code == 201
    assert _tip(a) == main["hash"]

    for block in (main, side):
        resp = a.get(f"/blocks/{block['hash']}")
        assert resp.status_code == 200
        assert resp.get_json() == block
    assert a.get(f"/blocks/{'f' * 64}").status_code == 404


def test_walk_back_fetches_several_missing_ancestors_by_hash(net):
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})
    _mine(b)  # B's own fork block at height 1
    a1, a2, a3 = _mine(a), _mine(a), _mine(a)  # A has no peers yet, so B hears nothing
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200

    assert b.post("/blocks/new", json=a3).status_code == 201
    assert _tip(b) == a3["hash"]
    assert net.gets == [f"/blocks/{a2['hash']}", f"/blocks/{a1['hash']}"]  # no full sync
    metrics = b.get("/metrics").get_json()
    assert (metrics["blocks_received"], metrics["reorgs"], metrics["blocks_rejected"]) == (1, 1, 0)


def test_walk_back_deeper_than_the_limit_falls_back_to_full_sync(net, monkeypatch):
    monkeypatch.setattr(sync_module, "MAX_ANCESTOR_DEPTH", 2)
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})
    a_blocks = [_mine(a) for _ in range(4)]  # B is missing 3 ancestors of the last one
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200

    assert b.post("/blocks/new", json=a_blocks[-1]).status_code == 201
    assert _tip(b) == a_blocks[-1]["hash"]
    assert net.gets == [f"/blocks/{a_blocks[2]['hash']}", f"/blocks/{a_blocks[1]['hash']}", "/chain"]


def test_invalid_block_with_unknown_parent_is_rejected_without_fetching(net):
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})
    _mine(a)
    a2 = _mine(a)
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200

    bad = dict(a2, nonce=a2["nonce"] + 1)  # its hash no longer matches: bad proof-of-work
    assert b.post("/blocks/new", json=bad).status_code == 400
    assert net.gets == []


def test_incremental_sync_walks_back_when_our_tip_is_on_another_fork(net):
    a = create_app(_cfg(5001)).test_client()
    net.clients["node-a"] = a
    a1, a2, a3 = _mine(a), _mine(a), _mine(a)

    ours = Blockchain()
    x1 = _block_on(ours.last_block)
    x2 = _block_on(x1)
    for block in (x1, x2):
        assert ours.add_block(block, DIFFICULTY)

    # Our tip index is 2, so A sends only a3; its parents a2 and a1 come by hash.
    assert Synchronizer(ours, DIFFICULTY).sync_incremental({"node-a"}) is True
    assert ours.last_block.hash == a3["hash"]
    assert net.gets == ["/blocks/since/2", f"/blocks/{a2['hash']}", f"/blocks/{a1['hash']}"]


# ------------------------------------------------------------------ #
#  /mine is honest when its block loses a race (B-18)
# ------------------------------------------------------------------ #


def test_mine_returns_409_when_a_competing_block_wins_the_race(net, monkeypatch):
    private_key, public_key = generate_keypair()
    wallet = public_key_to_pem(public_key)
    a = create_app(_cfg(5001)).test_client()
    b_cfg = _cfg(5002)
    b_cfg.MINER_ADDRESS = wallet  # B's first reward funds the transaction below
    b = create_app(b_cfg).test_client()
    net.clients.update({"node-a": a, "node-b": b})

    funding = _mine(b)
    assert a.post("/blocks/new", json=funding).status_code == 201
    tx = Transaction(sender=wallet, receiver="someone", amount=5.0)
    tx.sign(private_key)
    assert b.post("/transactions/new", json=tx.to_dict()).status_code == 201  # pending on B only
    rival = _mine(a)  # height 2; A has no peers, so B hasn't seen it
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200

    real_mine = ProofOfWork.mine

    def mine_while_rival_arrives(pow_engine, block):
        assert b.post("/blocks/new", json=rival).status_code == 201  # the rival takes height 2 first
        return real_mine(pow_engine, block)

    monkeypatch.setattr(ProofOfWork, "mine", mine_while_rival_arrives)
    resp = b.post("/mine")

    assert resp.status_code == 409
    stale = resp.get_json()["block"]
    assert (stale["index"], stale["previous_hash"]) == (rival["index"], rival["previous_hash"])
    assert tx.tx_id in [t["tx_id"] for t in stale["transactions"]]
    assert _tip(b) == rival["hash"]
    assert tx.tx_id in [t["tx_id"] for t in b.get("/mempool").get_json()["transactions"]]
    assert b.get("/metrics").get_json()["blocks_mined"] == 1  # only the funding block
    assert a.get(f"/blocks/{stale['hash']}").status_code == 404  # never broadcast


# ------------------------------------------------------------------ #
#  Each node checks coinbases against its configured reward (B-14)
# ------------------------------------------------------------------ #


def test_node_checks_coinbases_against_its_configured_reward(net):
    a_cfg = _cfg(5001)
    a_cfg.MINING_REWARD = 25.0
    a = create_app(a_cfg).test_client()
    b = create_app(_cfg(5002)).test_client()  # reward 10.0

    block = _mine(a)  # A accepts its own 25-coin coinbase
    assert b.post("/blocks/new", json=block).status_code == 400
    assert b.get("/metrics").get_json()["blocks_rejected"] == 1
