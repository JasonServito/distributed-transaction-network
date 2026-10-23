import threading
import time
import types

import requests
from flask import Flask, jsonify, request

from blockchain.block import Block
from blockchain.chain import Blockchain
from blockchain.transaction import Transaction
from consensus.proof_of_work import ProofOfWork
from mempool.pool import Mempool
from sync.synchronizer import Synchronizer
from utils.logger import get_logger

logger = get_logger(__name__)


def create_app(cfg: types.ModuleType) -> Flask:
    app = Flask(__name__)

    blockchain = Blockchain()
    mempool = Mempool()
    peers: set = set()
    pow_engine = ProofOfWork(cfg.DIFFICULTY)
    synchronizer = Synchronizer(blockchain, cfg.DIFFICULTY)

    # ------------------------------------------------------------------
    # Per-node monitoring counters
    # ------------------------------------------------------------------
    _metrics = {
        "start_time": time.time(),
        "blocks_mined": 0,  # blocks this node successfully mined
        "blocks_received": 0,  # new blocks accepted from peers
        "blocks_rejected": 0,  # blocks rejected (invalid, or missing parents not found)
        "reorgs": 0,  # chain reorganisations triggered
        "sync_calls": 0,  # catch-up rounds initiated (startup sync, missing parents)
        "tx_submitted": 0,  # transactions accepted into mempool
    }
    _metrics_lock = threading.Lock()

    def _inc(key: str, amount: int = 1) -> None:
        with _metrics_lock:
            _metrics[key] += amount

    if cfg.SEED_PEER:
        seed = cfg.SEED_PEER.strip()
        peers.add(seed)
        synchronizer.register_with_peer(seed, f"{cfg.HOST_ADDR}:{cfg.PORT}")
        # Use incremental sync on start-up: ask for only the blocks we lack.
        # Falls back to full sync automatically when the peer's chain is large
        # and the /blocks/since endpoint returns many results.
        synchronizer.sync_incremental(peers)
        _inc("sync_calls")

    def _broadcast(endpoint: str, payload: dict) -> None:
        def _send(peer: str) -> None:
            try:
                requests.post(f"http://{peer}{endpoint}", json=payload, timeout=3)
            except requests.RequestException as e:
                logger.warning("broadcast to %s failed: %s", peer, e)

        for peer in list(peers):
            threading.Thread(target=_send, args=(peer,), daemon=True).start()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @app.route("/chain", methods=["GET"])
    def get_chain():
        return jsonify(blockchain.to_dict()), 200

    @app.route("/peers", methods=["GET"])
    def get_peers():
        return jsonify({"peers": sorted(peers)}), 200

    @app.route("/peers/register", methods=["POST"])
    def register_peer():
        data = request.get_json(silent=True) or {}
        address = data.get("address", "").strip()
        if not address:
            return jsonify({"error": "address required"}), 400
        peers.add(address)
        logger.info("peer registered: %s", address)
        return jsonify({"peers": sorted(peers)}), 200

    @app.route("/transactions/new", methods=["POST"])
    def new_transaction():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        try:
            tx = Transaction.from_dict(data)
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        if tx.sender != "COINBASE":
            balance = blockchain.get_balance(tx.sender)
            if balance < tx.amount:
                return jsonify(
                    {
                        "error": "insufficient balance",
                        "balance": round(balance, 8),
                        "required": tx.amount,
                    }
                ), 400
        if mempool.add_transaction(tx):
            _inc("tx_submitted")
            _broadcast("/transactions/new", tx.to_dict())
            return jsonify({"tx_id": tx.tx_id}), 201
        return jsonify({"error": "transaction rejected"}), 400

    @app.route("/blocks/new", methods=["POST"])
    def receive_block():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        try:
            block = Block.from_dict(data)
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400

        # Short-circuit duplicates: do NOT re-broadcast a block we already have.
        # Without this check, receiving a known block would trigger a new broadcast
        # round to all peers, causing an infinite gossip storm.
        if blockchain.has_block(block.hash):
            return jsonify({"message": f"block {block.index} already known"}), 200

        # Taken before any catch-up, so a reorg caused by fetched ancestors is detected too.
        old_chain_hashes = blockchain.get_chain_block_hashes()
        if blockchain.has_block(block.previous_hash):
            accepted = blockchain.add_block(block, cfg.DIFFICULTY)
        else:
            # Parent unknown (e.g. it is still in flight, or we are on another fork):
            # fetch the missing ancestors from peers by hash, then add the block.
            _inc("sync_calls")
            accepted = synchronizer.add_with_ancestors(block, peers)

        if accepted:
            _inc("blocks_received")
            # Find blocks that were on the old chain but not the new one (reorg).
            # Their transactions must be returned to the mempool so they are not lost.
            orphaned_hashes = old_chain_hashes - blockchain.get_chain_block_hashes()
            if orphaned_hashes:
                _inc("reorgs")
                logger.info("reorg: %d block(s) orphaned after block %d", len(orphaned_hashes), block.index)
                for h in orphaned_hashes:
                    orphaned = blockchain.get_block_by_hash(h)
                    if orphaned:
                        for tx in orphaned.transactions:
                            if tx.sender != "COINBASE":
                                mempool.add_transaction(tx)
            mempool.remove_transactions([tx.tx_id for tx in block.transactions if tx.sender != "COINBASE"])
            _broadcast("/blocks/new", block.to_dict())
            return jsonify({"message": f"block {block.index} accepted"}), 201

        # Invalid block, or its ancestors could not be fetched.  Invalid blocks
        # trigger no fetching, so a bad peer can't make us sync.
        _inc("blocks_rejected")
        return jsonify({"error": "block rejected"}), 400

    @app.route("/mine", methods=["POST"])
    def mine():
        pending = mempool.get_transactions(limit=10)
        coinbase = Transaction(sender="COINBASE", receiver=cfg.MINER_ADDRESS, amount=cfg.MINING_REWARD)
        transactions = [coinbase] + pending

        block = Block(
            index=len(blockchain.chain),
            previous_hash=blockchain.last_block.hash,
            transactions=transactions,
        )
        pow_engine.mine(block)

        if not blockchain.add_block(block, cfg.DIFFICULTY):
            return jsonify({"error": "mined block rejected — chain moved on"}), 409

        # add_block also accepts side-branch blocks.  If another block took this
        # height while we were mining, ours lost the race: keep the pending
        # transactions, and don't broadcast or count the stale block.
        if block.hash not in blockchain.get_chain_block_hashes():
            return jsonify(
                {
                    "error": "mined block is on a side branch (another block won the race)",
                    "block": block.to_dict(),
                }
            ), 409

        _inc("blocks_mined")
        mempool.remove_transactions([tx.tx_id for tx in pending])
        _broadcast("/blocks/new", block.to_dict())
        return jsonify({"block": block.to_dict()}), 201

    @app.route("/mempool", methods=["GET"])
    def get_mempool():
        return jsonify({"transactions": mempool.to_list(), "size": mempool.size()}), 200

    @app.route("/blocks/since/<int:since_index>", methods=["GET"])
    def blocks_since(since_index: int):
        """
        Incremental sync endpoint.

        Returns every block on the main chain whose index is strictly greater
        than *since_index*.  A joining node calls this with its current tip
        index so it only downloads what it is missing, not the whole chain.

        Example:  GET /blocks/since/42  → blocks 43, 44, 45, ...
        """
        blocks = [b.to_dict() for b in blockchain.chain if b.index > since_index]
        return jsonify({"blocks": blocks, "count": len(blocks)}), 200

    @app.route("/blocks/<block_hash>", methods=["GET"])
    def get_block(block_hash: str):
        """
        One block by hash, from the main chain or a side branch (404 if unknown).

        A node that receives a block whose parent it lacks walks back through
        this endpoint, one ancestor at a time, until it reaches a block it knows.
        """
        block = blockchain.get_block_by_hash(block_hash)
        if block is None:
            return jsonify({"error": "block not found"}), 404
        return jsonify(block.to_dict()), 200

    @app.route("/status", methods=["GET"])
    def status():
        return jsonify(
            {
                "chain_length": len(blockchain.chain),
                "mempool_size": mempool.size(),
                "peers": sorted(peers),
                "difficulty": cfg.DIFFICULTY,
            }
        ), 200

    @app.route("/balance", methods=["GET"])
    def get_balance():
        """
        Query confirmed on-chain balance for an address.

        Usage:  GET /balance?address=<address>

        The address is a PEM public key for real wallets, or a plain label
        such as 'miner_default' for the default miner reward address.
        Balance is computed by summing all credits minus all debits recorded
        on the main chain.

        Example:
            curl "http://127.0.0.1:5000/balance?address=miner_default"
        """
        address = request.args.get("address", "").strip()
        if not address:
            return jsonify({"error": "address query param required"}), 400
        balance = blockchain.get_balance(address)
        return jsonify({"address": address, "balance": round(balance, 8)}), 200

    @app.route("/metrics", methods=["GET"])
    def metrics():
        """
        Per-node performance and health metrics.

        Useful for comparing nodes during a demo (e.g. which node detected
        a reorg, how many syncs were needed after a crash).

        Returns
        -------
        uptime_seconds      : seconds since this node process started
        blocks_mined        : blocks this node produced via /mine
        blocks_received     : new blocks accepted from peers via /blocks/new
        blocks_rejected     : blocks turned away (invalid, or missing parents not found)
        reorgs              : chain reorganisations (longer fork took over)
        sync_calls          : catch-up rounds this node initiated (startup sync, missing parents)
        tx_submitted        : transactions accepted into the local mempool
        chain_length        : current canonical chain height
        known_blocks_total  : all blocks ever seen (main chain + side branches)
        fork_pool_size      : side-chain / orphan blocks not on main chain
        peer_count          : number of registered peers
        """
        with _metrics_lock:
            snapshot = dict(_metrics)

        chain_len = len(blockchain.chain)
        known_total = blockchain.known_block_count()

        return jsonify(
            {
                "uptime_seconds": round(time.time() - snapshot["start_time"], 2),
                "blocks_mined": snapshot["blocks_mined"],
                "blocks_received": snapshot["blocks_received"],
                "blocks_rejected": snapshot["blocks_rejected"],
                "reorgs": snapshot["reorgs"],
                "sync_calls": snapshot["sync_calls"],
                "tx_submitted": snapshot["tx_submitted"],
                "chain_length": chain_len,
                "known_blocks_total": known_total,
                "fork_pool_size": known_total - chain_len,
                "peer_count": len(peers),
            }
        ), 200

    return app
