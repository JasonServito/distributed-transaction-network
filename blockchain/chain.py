import threading
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Set, Tuple

from blockchain.block import Block
from blockchain.transaction import Transaction
from utils.logger import get_logger

logger = get_logger(__name__)


class Blockchain:
    def __init__(self, mining_reward: float = 10.0) -> None:
        self._lock = threading.Lock()
        # What every block's coinbase must pay (the default matches config.MINING_REWARD's).
        self.mining_reward = float(mining_reward)
        self.chain: List[Block] = []
        # Every valid block we have ever seen, keyed by its hash.
        # This covers both the main chain AND any side-chain / orphan blocks.
        self._block_store: Dict[str, Block] = {}
        self._create_genesis_block()

    def _create_genesis_block(self) -> None:
        genesis = Block(index=0, previous_hash="0" * 64, transactions=[], timestamp=0.0)
        genesis.hash = genesis.compute_hash()
        self.chain.append(genesis)
        self._block_store[genesis.hash] = genesis
        logger.info("genesis block: %s", genesis.hash)

    @property
    def last_block(self) -> Block:
        return self.chain[-1]

    def __len__(self) -> int:
        return len(self.chain)

    # ------------------------------------------------------------------
    # Block ingestion
    # ------------------------------------------------------------------

    def add_block(self, block: Block, difficulty: int) -> bool:
        """
        Accept a block into the block store and trigger chain selection.

        A block is accepted when:
          1. Its PoW is valid.
          2. All its transactions are valid, and exactly one is a coinbase:
             the first, paying the mining reward.
          3. Its parent (previous_hash) is already known — either on the
             main chain or in a side branch.  This allows us to store fork
             blocks rather than discarding them.
          4. It is valid on top of its parent (see _check_on_parent).

        After acceptance, _update_main_chain() walks all known branch tips
        and switches self.chain to the longest valid path.
        """
        with self._lock:
            problem = self.check_block(block, difficulty)
            if problem:
                logger.warning("block %d rejected: %s", block.index, problem)
                return False

            # Accept the block if its parent is known (main chain OR fork pool)
            if block.previous_hash not in self._block_store:
                logger.warning(
                    "block %d rejected: unknown parent %s",
                    block.index,
                    block.previous_hash[:12],
                )
                return False

            # Silently deduplicate
            if block.hash in self._block_store:
                logger.debug("block %d already known, skipping", block.index)
                return True

            problem = self._check_on_parent(block)
            if problem:
                logger.warning("block %d rejected: %s", block.index, problem)
                return False

            self._block_store[block.hash] = block
            logger.info(
                "block %d stored  hash=%.12s  parent=%.12s",
                block.index,
                block.hash,
                block.previous_hash,
            )

            # Reselect the canonical chain (may trigger a reorg)
            self._update_main_chain()
            return True

    # ------------------------------------------------------------------
    # Fork / chain-selection logic
    # ------------------------------------------------------------------

    def _find_tips(self) -> List[str]:
        """
        A 'tip' is any block that no other block references as its parent.
        These are the candidate chain heads we compare when selecting the
        longest chain.
        """
        referenced_as_parent = {b.previous_hash for b in self._block_store.values()}
        return [h for h in self._block_store if h not in referenced_as_parent]

    def _build_chain_to_genesis(self, tip_hash: str) -> List[Block]:
        """
        Walk backward from tip_hash through _block_store until we reach the
        genesis block (whose previous_hash is all zeros).
        Returns the blocks in forward order [genesis, ..., tip].
        Returns an empty list if the path is broken (orphan with missing ancestor).
        """
        path: List[Block] = []
        current = tip_hash
        visited: set = set()

        while current in self._block_store:
            if current in visited:  # cycle guard — should never happen
                logger.error("cycle detected in block store at %s", current[:12])
                return []
            visited.add(current)
            block = self._block_store[current]
            path.append(block)
            if block.previous_hash == "0" * 64:
                break  # reached genesis
            current = block.previous_hash

        path.reverse()
        return path

    def _update_main_chain(self) -> None:
        """
        Compare every tip's chain length and promote the longest one to
        self.chain.  Ties are broken by keeping the current chain (first
        seen wins), which matches Bitcoin's behaviour.
        """
        best_length = len(self.chain)
        best_chain: List[Block] = []

        for tip_hash in self._find_tips():
            candidate = self._build_chain_to_genesis(tip_hash)
            if len(candidate) > best_length:
                best_length = len(candidate)
                best_chain = candidate

        if best_chain:
            old_tip = self.chain[-1].hash if self.chain else "none"
            self.chain = best_chain
            logger.info(
                "chain reorg: length=%d  old_tip=%.12s  new_tip=%.12s",
                len(self.chain),
                old_tip,
                self.chain[-1].hash,
            )

    def has_block(self, block_hash: str) -> bool:
        """Return True if we have already ingested this block."""
        with self._lock:
            return block_hash in self._block_store

    def get_block_by_hash(self, block_hash: str):
        """Return a block from the store by hash, or None if not found."""
        with self._lock:
            return self._block_store.get(block_hash)

    def known_block_count(self) -> int:
        """Return how many blocks we know: main chain plus side branches."""
        with self._lock:
            return len(self._block_store)

    def get_chain_block_hashes(self) -> set:
        """Return the set of block hashes currently on the main chain."""
        with self._lock:
            return {b.hash for b in self.chain}

    def get_chain_tx_ids(self) -> set:
        """Return the set of tx_ids currently on the main chain."""
        with self._lock:
            return {tx.tx_id for b in self.chain for tx in b.transactions}

    def get_balance(self, address: str) -> float:
        """
        Compute the confirmed balance for *address* by scanning the main chain.

        Sums all amounts received minus all amounts sent.  Coinbase transactions
        are credited to their receiver like any other transaction.
        """
        with self._lock:
            balance = 0.0
            for block in self.chain:
                for tx in block.transactions:
                    if tx.receiver == address:
                        balance += tx.amount
                    if tx.sender == address:
                        balance -= tx.amount
            return balance

    def select_transactions(
        self, parent_hash: str, coinbase: Transaction, pending: List[Transaction]
    ) -> List[Transaction]:
        """
        Pick the transactions for a new block on *parent_hash*: *coinbase*,
        then each pending tx that is valid after the ones before it.  Skips
        any that is a coinbase, already on that branch, or not affordable,
        so add_block won't reject the block because of them.
        """
        with self._lock:
            balances, tx_ids = self._branch_state(parent_hash)
            self._apply(coinbase, balances, tx_ids)
            selected = [coinbase]
            for tx in pending:
                if tx.sender != "COINBASE" and self._apply(tx, balances, tx_ids) is None:
                    selected.append(tx)
            return selected

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def check_block(self, block: Block, difficulty: int) -> Optional[str]:
        """
        Return why *block* is invalid on its own (without looking at its
        parent), or None if it is valid.  Lets callers reject a bad block
        before fetching any of its missing ancestors.
        """
        if not self._valid_proof(block, difficulty):
            return "bad proof-of-work"
        if not all(tx.is_valid() for tx in block.transactions):
            return "invalid transaction"
        coinbase_positions = [i for i, tx in enumerate(block.transactions) if tx.sender == "COINBASE"]
        if coinbase_positions != [0]:
            return "a block needs exactly one coinbase transaction, and it must be first"
        if float(block.transactions[0].amount) != self.mining_reward:
            return f"coinbase pays {block.transactions[0].amount}, not the reward {self.mining_reward}"
        return None

    def _check_on_parent(self, block: Block) -> Optional[str]:
        """
        Return why *block* is invalid on top of its parent, or None if it is
        valid.  The rules use the branch the block extends, not the main
        chain, so valid fork blocks are accepted.  The parent must be known,
        and the caller holds the lock.
        """
        parent = self._block_store[block.previous_hash]
        if block.index != parent.index + 1:
            return f"index {block.index} does not follow parent index {parent.index}"
        balances, tx_ids = self._branch_state(parent.hash)
        for tx in block.transactions:
            problem = self._apply(tx, balances, tx_ids)
            if problem:
                return problem
        return None

    def _branch_state(self, tip_hash: str) -> Tuple[DefaultDict[str, float], Set[str]]:
        """
        Replay the branch from genesis to *tip_hash* into balances per
        address and the set of tx_ids on it.  The caller holds the lock.
        """
        balances: DefaultDict[str, float] = defaultdict(float)
        tx_ids: Set[str] = set()
        for block in self._build_chain_to_genesis(tip_hash):
            for tx in block.transactions:
                balances[tx.receiver] += tx.amount
                balances[tx.sender] -= tx.amount
                tx_ids.add(tx.tx_id)
        return balances, tx_ids

    @staticmethod
    def _apply(tx: Transaction, balances: DefaultDict[str, float], tx_ids: Set[str]) -> Optional[str]:
        """
        Apply *tx* to a branch's *balances* and *tx_ids*, after the
        transactions before it.  Returns why it can't be applied (changing
        nothing), or None.
        """
        if tx.tx_id in tx_ids:
            return f"tx {tx.tx_id} is already on this branch"
        if tx.sender != "COINBASE" and balances[tx.sender] < tx.amount:
            return f"tx {tx.tx_id} spends {tx.amount} but its sender has {balances[tx.sender]}"
        balances[tx.receiver] += tx.amount
        balances[tx.sender] -= tx.amount
        tx_ids.add(tx.tx_id)
        return None

    @staticmethod
    def _valid_proof(block: Block, difficulty: int) -> bool:
        return block.hash == block.compute_hash() and block.hash.startswith("0" * difficulty)

    def is_valid_chain(self, difficulty: int) -> bool:
        if not self.chain:
            return False
        for i in range(1, len(self.chain)):
            cur, prev = self.chain[i], self.chain[i - 1]
            if cur.hash != cur.compute_hash():
                return False
            if cur.previous_hash != prev.hash:
                return False
            if not cur.hash.startswith("0" * difficulty):
                return False
            if not all(tx.is_valid() for tx in cur.transactions):
                return False
        return True

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"chain": [b.to_dict() for b in self.chain], "length": len(self.chain)}

    @classmethod
    def from_dict(cls, d: dict, mining_reward: float = 10.0) -> "Blockchain":
        instance = cls.__new__(cls)
        instance._lock = threading.Lock()
        instance.mining_reward = float(mining_reward)
        instance._block_store = {}
        instance.chain = [Block.from_dict(b) for b in d["chain"]]
        # Rebuild the block store from the flat chain list
        for b in instance.chain:
            instance._block_store[b.hash] = b
        return instance
