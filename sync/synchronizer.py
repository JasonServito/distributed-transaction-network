from typing import List, Optional

import requests

from blockchain.block import Block
from blockchain.chain import Blockchain
from utils.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = 5


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
        Download and validate the complete chain from every peer, keep the
        longest valid one.  Used on first boot or as a fallback when
        incremental sync cannot bridge the gap (e.g. peer is far ahead).
        """
        best_chain = None
        best_length = len(self.blockchain.chain)

        for peer in list(peers):
            data = self._fetch_chain(peer)
            if not data:
                continue
            candidate = Blockchain.from_dict(data)
            if len(candidate.chain) > best_length and candidate.is_valid_chain(self.difficulty):
                best_length = len(candidate.chain)
                best_chain = candidate

        if best_chain:
            self.blockchain.chain = best_chain.chain
            # Rebuild block store so fork logic stays consistent
            self.blockchain._block_store = {b.hash: b for b in self.blockchain.chain}
            logger.info("chain replaced via full sync (length=%d)", len(self.blockchain.chain))
            return True

        logger.info("chain is already up to date")
        return False

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
                if self.blockchain.add_block(block, self.difficulty):
                    added_any = True

        return added_any
