"""Fork healing and catch-up between nodes.

Real nodes (``create_app``) talk through Flask test clients instead of sockets:
``requests`` inside the node, synchronizer and wallet modules is swapped for an
in-memory transport, and gossip threads run inline, so the tests are deterministic.
"""

import threading
import types

import pytest
import requests
from flask import Flask

import network.node as node_module
import sync.synchronizer as sync_module
import wallet as wallet_module
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
        self.posts: list = []  # paths sent with POST, in order

    def _route(self, url: str):
        host, _, path = url.removeprefix("http://").partition("/")
        if host not in self.clients:
            raise requests.ConnectionError(f"no such node: {host}")
        return self.clients[host], "/" + path

    def get(self, url: str, params=None, **_kwargs) -> _Response:
        client, path = self._route(url)
        self.gets.append(path)
        return _Response(client.get(path, query_string=params))

    def post(self, url: str, json=None, **_kwargs) -> _Response:
        client, path = self._route(url)
        self.posts.append(path)
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
    cfg.MINER_PUBKEY_FILE = ""  # type: ignore[attr-defined]
    cfg.MINING_REWARD = 10.0  # type: ignore[attr-defined]
    return cfg


@pytest.fixture
def net(monkeypatch):
    transport = _Transport()
    monkeypatch.setattr(node_module, "requests", transport)
    monkeypatch.setattr(sync_module, "requests", transport)
    monkeypatch.setattr(wallet_module, "requests", transport)
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


# ------------------------------------------------------------------ #
#  Mempool admission and cleanup (B-17)
# ------------------------------------------------------------------ #


def _new_wallet():
    """A key pair: (private key, address)."""
    private_key, public_key = generate_keypair()
    return private_key, public_key_to_pem(public_key)


def _pay(wallet, amount: float) -> dict:
    """A signed transaction from *wallet*, as the JSON a client would submit."""
    private_key, address = wallet
    tx = Transaction(sender=address, receiver="someone", amount=amount)
    tx.sign(private_key)
    return tx.to_dict()


def _node_mining_to(port: int, address: str):
    cfg = _cfg(port)
    cfg.MINER_ADDRESS = address  # type: ignore[attr-defined]
    return create_app(cfg).test_client()


def _pending_ids(client) -> list:
    return [t["tx_id"] for t in client.get("/mempool").get_json()["transactions"]]


def test_tx_that_overspends_with_pending_ones_is_rejected(net):
    wallet = _new_wallet()
    a = _node_mining_to(5001, wallet[1])
    _mine(a)  # the wallet has 10

    assert a.post("/transactions/new", json=_pay(wallet, 6.0)).status_code == 201
    resp = a.post("/transactions/new", json=_pay(wallet, 6.0))  # 6 of the 10 are already pending
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "insufficient balance"


def test_confirmed_tx_resubmitted_is_rejected(net):
    wallet = _new_wallet()
    a = _node_mining_to(5001, wallet[1])
    _mine(a)
    tx = _pay(wallet, 5.0)
    assert a.post("/transactions/new", json=tx).status_code == 201
    _mine(a)  # confirms tx; the wallet, also the miner, can still afford it

    resp = a.post("/transactions/new", json=tx)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "transaction already confirmed"
    assert _pending_ids(a) == []


def test_txs_in_ancestors_fetched_by_walk_back_leave_the_mempool(net):
    wallet = _new_wallet()
    a = _node_mining_to(5001, wallet[1])
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})
    funding = _mine(a)
    assert b.post("/blocks/new", json=funding).status_code == 201
    tx = _pay(wallet, 5.0)
    for node in (a, b):
        assert node.post("/transactions/new", json=tx).status_code == 201

    a2, a3 = _mine(a), _mine(a)  # A has no peers, so B hears nothing
    assert tx["tx_id"] in [t["tx_id"] for t in a2["transactions"]]
    assert b.post("/peers/register", json={"address": "node-a"}).status_code == 200
    assert b.post("/blocks/new", json=a3).status_code == 201  # a2 comes by walk-back

    assert _tip(b) == a3["hash"]
    assert _pending_ids(b) == []


def test_reorg_does_not_return_txs_that_are_on_the_new_main_chain(net):
    wallet = _new_wallet()
    funding = _mine(_node_mining_to(5003, wallet[1]))
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    tx = _pay(wallet, 5.0)
    for node in (a, b):
        assert node.post("/blocks/new", json=funding).status_code == 201
        assert node.post("/transactions/new", json=tx).status_code == 201

    _mine(b)  # B confirms tx on its own branch
    a_blocks = [_mine(a) for _ in range(2)]  # A confirms it too, in its first block
    for block in a_blocks:
        assert b.post("/blocks/new", json=block).status_code == 201

    assert _tip(b) == a_blocks[-1]["hash"]  # B's block left the main chain
    assert _pending_ids(b) == []


def test_mine_skips_and_evicts_an_unaffordable_pending_tx(net):
    wallet = _new_wallet()
    funding = _mine(_node_mining_to(5003, wallet[1]))
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    assert b.post("/blocks/new", json=funding).status_code == 201
    tx = _pay(wallet, 5.0)
    assert b.post("/transactions/new", json=tx).status_code == 201
    _mine(b)  # confirms tx on the only branch that funds the wallet

    a_blocks = [_mine(a) for _ in range(3)]  # longer, and never funds the wallet
    for block in a_blocks:
        assert b.post("/blocks/new", json=block).status_code == 201
    assert _tip(b) == a_blocks[-1]["hash"]
    assert _pending_ids(b) == [tx["tx_id"]]  # back from the orphaned block, now unaffordable

    mined = _mine(b)
    assert [t["sender"] for t in mined["transactions"]] == ["COINBASE"]
    assert _pending_ids(b) == []
    assert a.post("/blocks/new", json=mined).status_code == 201  # valid for other nodes too


# ------------------------------------------------------------------ #
#  Only miners create coinbase transactions (B-17a)
# ------------------------------------------------------------------ #


def test_coinbase_tx_submitted_by_a_client_is_rejected_and_not_relayed(net):
    a = create_app(_cfg(5001)).test_client()
    b = create_app(_cfg(5002)).test_client()
    net.clients.update({"node-a": a, "node-b": b})
    assert a.post("/peers/register", json={"address": "node-b"}).status_code == 200
    tx = Transaction(sender="COINBASE", receiver="someone", amount=10.0)

    resp = a.post("/transactions/new", json=tx.to_dict())

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "coinbase transactions are created by miners only"}
    assert _pending_ids(a) == []
    assert net.posts == []  # not gossiped to B
    assert a.get("/metrics").get_json()["tx_submitted"] == 0


# ------------------------------------------------------------------ #
#  Wallet CLI and mining to a wallet (B-19)
# ------------------------------------------------------------------ #


def _keygen(tmp_path, name: str) -> tuple:
    """(key file, pub file) of a new wallet, as the strings a CLI user would pass."""
    key_file, pub_file = wallet_module.keygen(name, str(tmp_path))
    return str(key_file), str(pub_file)


def _node_mining_to_wallet(port: int, pub_file: str):
    cfg = _cfg(port)
    cfg.MINER_PUBKEY_FILE = pub_file  # type: ignore[attr-defined]
    return create_app(cfg).test_client()


def test_wallet_payment_gives_the_same_balances_on_every_node(net, tmp_path):
    alice_key, alice_pub = _keygen(tmp_path, "alice")
    _, bob_pub = _keygen(tmp_path, "bob")
    nodes = {
        "node-a": _node_mining_to_wallet(5001, alice_pub),
        "node-b": create_app(_cfg(5002)).test_client(),
        "node-c": create_app(_cfg(5003)).test_client(),
    }
    net.clients.update(nodes)
    for name, client in nodes.items():
        for other in nodes.keys() - {name}:
            assert client.post("/peers/register", json={"address": other}).status_code == 200

    _mine(nodes["node-a"])
    _mine(nodes["node-a"])  # alice has two rewards
    tx_id = wallet_module.send(alice_key, bob_pub, 4, node="node-a")  # an int amount must sign correctly
    mined = _mine(nodes["node-b"])  # another node confirms the payment

    assert tx_id in [t["tx_id"] for t in mined["transactions"]]
    alice, bob = wallet_module.read_address(alice_pub), wallet_module.read_address(bob_pub)
    for name, client in nodes.items():
        assert wallet_module.balance(alice, name) == 16.0  # 2 x 10 - 4
        assert wallet_module.balance(bob, name) == 4.0
        assert wallet_module.balance("miner_5002", name) == 10.0
        assert _tip(client) == mined["hash"]
        assert _pending_ids(client) == []


def test_cli_sends_to_a_plain_label(net, tmp_path, capsys):
    alice_key, alice_pub = _keygen(tmp_path, "alice")
    a = _node_mining_to_wallet(5001, alice_pub)
    net.clients["node-a"] = a
    _mine(a)

    send_args = ["send", "--key", alice_key, "--to", "carol", "--amount", "3", "--node", "node-a"]
    assert wallet_module.main(send_args) == 0
    tx_id = capsys.readouterr().out.strip()
    mined = _mine(a)  # alice also gets this reward

    assert tx_id in [t["tx_id"] for t in mined["transactions"]]
    assert wallet_module.main(["balance", "--address", "carol", "--node", "node-a"]) == 0
    assert capsys.readouterr().out == "3.0\n"
    assert wallet_module.main(["balance", "--pub", alice_pub, "--node", "node-a"]) == 0
    assert capsys.readouterr().out == "17.0\n"  # 2 x 10 - 3


def test_cli_send_beyond_the_balance_prints_the_node_error_and_exits_nonzero(net, tmp_path, capsys):
    alice_key, alice_pub = _keygen(tmp_path, "alice")
    _, bob_pub = _keygen(tmp_path, "bob")
    a = _node_mining_to_wallet(5001, alice_pub)
    net.clients["node-a"] = a
    _mine(a)  # alice has 10

    send_args = ["send", "--key", alice_key, "--to", bob_pub, "--amount", "30", "--node", "node-a"]

    assert wallet_module.main(send_args) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "error: insufficient balance (balance=10.0, pending=0, required=30.0)\n"
    assert _pending_ids(a) == []


def test_keygen_refuses_to_overwrite_a_wallet(tmp_path, capsys):
    key_file, pub_file = wallet_module.keygen("alice", str(tmp_path))
    before = (key_file.read_text(), pub_file.read_text())

    with pytest.raises(FileExistsError):
        wallet_module.keygen("alice", str(tmp_path))
    assert wallet_module.main(["keygen", "alice", "--dir", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert (key_file.read_text(), pub_file.read_text()) == before

    key_file.unlink()
    with pytest.raises(FileExistsError):  # the .pub alone is enough to refuse
        wallet_module.keygen("alice", str(tmp_path))
    assert not key_file.exists()


def test_node_refuses_a_private_key_as_its_miner_pubkey_file(tmp_path):
    key_file, _ = _keygen(tmp_path, "alice")
    with pytest.raises(ValueError, match="not a PEM public key"):
        _node_mining_to_wallet(5001, key_file)
