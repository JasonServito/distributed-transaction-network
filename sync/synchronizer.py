from typing import List, Optional

import requests

from blockchain.block import Block
from blockchain.chain import Blockchain
from utils.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = 5

# Most missing ancestors we fetch one by one before falling back to a full sync.
MAX_ANCESTOR_DEPTH = 50


class Synchronizer:
    def __init__(self, blockchain: Blockchain, difficulty: int) -> None:
        self.blockchain = blockchain
        self.difficulty = difficulty

    # ------------------------------------------------------------------
    # Low-level fetch helpers
    # ------------------------------------------------------------------

    def _fetch_chain(self, peer: str) -> Optional[dict]:
        """Download the full chain from a peer (used for bootstrap / fallback)."""
        try:
            resp = requests.get(f"http://{peer}/chain", timeout=_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as e:
            logger.warning("could not reach %s: %s", peer, e)
        return None

    def _fetch_blocks_since(self, peer: str, since_index: int) -> Optional[List[dict]]:
        """
        Ask a peer for every block whose index is strictly greater than
        since_index.  Returns a list of raw block dicts, an empty list if
        there are no new blocks, or None if the peer does not support the
        endpoint (404 / network error).
        """
        try:
            resp = requests.get(
                f"http://{peer}/blocks/since/{since_index}",
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json().get("blocks", [])
            if resp.status_code == 404:
                return None  # endpoint not supported — signal fallback
        except requests.RequestException as e:
            logger.warning("incremental fetch from %s failed: %s", peer, e)
        return None

    def _fetch_block(self, peer: str, block_hash: str) -> Optional[Block]:
        """Ask a peer for one block by hash.  None if it doesn't have it or can't be reached."""
        try:
            resp = requests.get(f"http://{peer}/blocks/{block_hash}", timeout=_TIMEOUT)
            if resp.status_code == 200:
                block = Block.from_dict(resp.json())
                if block.hash == block_hash:
                    return block
        except requests.RequestException as e:
            logger.warning("could not fetch block %.12s from %s: %s", block_hash, peer, e)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("malformed block from %s: %s", peer, e)
        return None

    def _fetch_block_from_any(self, peers: set, block_hash: str) -> Optional[Block]:
        """Try each peer in turn until one has the block."""
        for peer in list(peers):
            block = self._fetch_block(peer, block_hash)
            if block is not None:
                return block
        return None

    # ------------------------------------------------------------------
    # Peer registration
    # ------------------------------------------------------------------

    def register_with_peer(self, peer: str, self_address: str) -> bool:
        try:
            resp = requests.post(
                f"http://{peer}/peers/register",
                json={"address": self_address},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.info("registered with %s", peer)
                return True
        except requests.RequestException as e:
            logger.warning("could not register with %s: %s", peer, e)
        return False

    # ------------------------------------------------------------------
    # Full-chain sync  (bootstrap / fallback)
    # ------------------------------------------------------------------

    def sync_with_peers(self, peers: set) -> bool:
        """
        Download the complete chain from every peer and feed its blocks,
        oldest first, through add_block.  Used on first boot or as a fallback
        when incremental sync cannot bridge the gap (e.g. peer is far ahead).

        add_block validates each block and picks the longest chain, so side
        branches we already hold are kept, and a peer's chain is not adopted
        past its first invalid block.  Returns True if our main chain changed.
        """
        old_tip = self.blockchain.last_block.hash

        for peer in list(peers):
            data = self._fetch_chain(peer)
            if not data:
                continue
            for raw in data.get("chain", []):
                try:
                    block = Block.from_dict(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("malformed block from %s: %s", peer, exc)
                    break
                if self.blockchain.has_block(block.hash):
                    continue  # genesis and blocks we already have
                if not self.blockchain.add_block(block, self.difficulty):
                    break  # later blocks build on this one, so stop here

        if self.blockchain.last_block.hash != old_tip:
            logger.info("main chain changed via full sync (length=%d)", len(self.blockchain))
            return True

        logger.info("chain is already up to date")
        return False

    # ------------------------------------------------------------------
    # Missing ancestors  (walk back by hash)
    # ------------------------------------------------------------------

    def add_with_ancestors(self, block: Block, peers: set) -> bool:
        """
        Add a block whose parent we don't know yet.

        Asks peers for the parent by hash, then for its parent, and so on
        until we reach a block we already have.  Then adds the fetched
        ancestors oldest first, and finally *block*, through add_block.
        Falls back to a full sync when more than MAX_ANCESTOR_DEPTH blocks
        are missing or no peer has a block we need.

        A block that is invalid on its own (bad PoW or transaction) is
        rejected before anything is fetched.  Returns True if *block* was
        accepted.
        """
        problem = self.blockchain.check_block(block, self.difficulty)
        if problem:
            logger.warning("block %d rejected: %s", block.index, problem)
            return False

        ancestors: List[Block] = []
        parent_hash = block.previous_hash
        while not self.blockchain.has_block(parent_hash):
            if len(ancestors) == MAX_ANCESTOR_DEPTH:
                logger.info("block %d: over %d missing ancestors, full sync", block.index, MAX_ANCESTOR_DEPTH)
                return self._full_sync_then_add(block, peers)
            parent = self._fetch_block_from_any(peers, parent_hash)
            if parent is None:
                logger.info("block %d: no peer has ancestor %.12s, full sync", block.index, parent_hash)
                return self._full_sync_then_add(block, peers)
            ancestors.append(parent)
            parent_hash = parent.previous_hash

        logger.info("block %d: fetched %d missing ancestor(s)", block.index, len(ancestors))
        for ancestor in reversed(ancestors):
            if not self.blockchain.add_block(ancestor, self.difficulty):
                return False
        return self.blockchain.add_block(block, self.difficulty)

    def _full_sync_then_add(self, block: Block, peers: set) -> bool:
        self.sync_with_peers(peers)
        return self.blockchain.add_block(block, self.difficulty)

    # ------------------------------------------------------------------
    # Incremental sync  (normal operation)
    # ------------------------------------------------------------------

    def sync_incremental(self, peers: set) -> bool:
        """
        Efficient incremental synchronisation.

        For each peer the node tells it the index of its current tip
        ("I have up to block N") and only downloads the missing blocks.
        This is O(missing blocks) instead of O(entire chain).

        If a peer does not support the /blocks/since endpoint (returns 404
        or a network error), we fall back to a full sync for that peer so
        that older nodes are still compatible.
        """
        added_any = False

        for peer in list(peers):
            since_index = len(self.blockchain.chain) - 1  # re-check after each peer

            new_blocks_raw = self._fetch_blocks_since(peer, since_index)

            if new_blocks_raw is None:
                # Peer does not support incremental endpoint — fall back
                logger.info("peer %s: /blocks/since not available, falling back to full sync", peer)
                if self.sync_with_peers({peer}):
                    added_any = True
                continue

            if not new_blocks_raw:
                logger.debug("peer %s: no new blocks since index %d", peer, since_index)
                continue

            logger.info(
                "peer %s: %d new block(s) since index %d",
                peer,
                len(new_blocks_raw),
                since_index,
            )

            for raw in new_blocks_raw:
                try:
                    block = Block.from_dict(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("malformed block from %s: %s", peer, exc)
                    continue
                if self.blockchain.has_block(block.previous_hash):
                    added = self.blockchain.add_block(block, self.difficulty)
                else:
                    # Parent unknown (e.g. we are on another fork): walk back by hash.
                    added = self.add_with_ancestors(block, peers)
                if added:
                    added_any = True

        return added_any
