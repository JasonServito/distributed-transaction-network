import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger
from utils.signing import pem_to_public_key, verify

logger = get_logger(__name__)


@dataclass
class Transaction:
    sender: str  # PEM public key, or "COINBASE"
    receiver: str
    amount: float
    timestamp: float = field(default_factory=time.time)
    tx_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signature: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "amount": self.amount,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls(
            sender=d["sender"],
            receiver=d["receiver"],
            amount=float(d["amount"]),
            timestamp=float(d["timestamp"]),
            tx_id=d["tx_id"],
            signature=d.get("signature"),
        )

    def _payload(self) -> str:
        return f"{self.sender}|{self.receiver}|{self.amount}|{self.timestamp}|{self.tx_id}"

    def sign(self, private_key) -> None:
        from utils.signing import sign as _sign

        self.signature = _sign(private_key, self._payload())

    def is_valid(self) -> bool:
        if self.amount <= 0:
            return False
        if self.sender == "COINBASE":
            return True
        if not self.signature:
            return False
        try:
            return verify(pem_to_public_key(self.sender), self._payload(), self.signature)
        except Exception:
            return False
