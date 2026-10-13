import threading
from typing import List

from blockchain.transaction import Transaction
from utils.logger import get_logger

logger = get_logger(__name__)


class Mempool:
    def __init__(self) -> None:
        self._pool: dict[str, Transaction] = {}
        self._lock = threading.Lock()

    def add_transaction(self, tx: Transaction) -> bool:
        with self._lock:
            if tx.tx_id in self._pool:
                return False
            if not tx.is_valid():
                logger.warning("rejected tx %s", tx.tx_id)
                return False
            self._pool[tx.tx_id] = tx
            logger.info("tx %s added (mempool size=%d)", tx.tx_id, len(self._pool))
            return True

    def remove_transactions(self, tx_ids: List[str]) -> None:
        with self._lock:
            for tx_id in tx_ids:
                self._pool.pop(tx_id, None)

    def get_transactions(self, limit: int = 10) -> List[Transaction]:
        with self._lock:
            return list(self._pool.values())[:limit]

    def size(self) -> int:
        with self._lock:
            return len(self._pool)

    def has_transaction(self, tx_id: str) -> bool:
        with self._lock:
            return tx_id in self._pool

    def to_list(self) -> List[dict]:
        with self._lock:
            return [tx.to_dict() for tx in self._pool.values()]
