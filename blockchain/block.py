import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import List

from blockchain.transaction import Transaction
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Block:
    index: int
    previous_hash: str
    transactions: List[Transaction]
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0
    hash: str = ""

    def compute_hash(self) -> str:
        # hash field is excluded so the digest is reproducible from the other fields
        data = {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "transactions": [tx.to_dict() for tx in self.transactions],
            "timestamp": self.timestamp,
            "nonce": self.nonce,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "transactions": [tx.to_dict() for tx in self.transactions],
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        block = cls(
            index=d["index"],
            previous_hash=d["previous_hash"],
            transactions=[Transaction.from_dict(tx) for tx in d["transactions"]],
            timestamp=float(d["timestamp"]),
            nonce=int(d["nonce"]),
        )
        block.hash = d["hash"]
        return block
