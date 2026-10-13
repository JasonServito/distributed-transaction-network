import time

from blockchain.block import Block
from utils.logger import get_logger

logger = get_logger(__name__)


class ProofOfWork:
    def __init__(self, difficulty: int) -> None:
        if difficulty < 1:
            raise ValueError("difficulty must be at least 1")
        self.difficulty = difficulty
        self.prefix = "0" * difficulty

    def mine(self, block: Block) -> Block:
        logger.info("mining block %d (difficulty=%d)...", block.index, self.difficulty)
        start = time.time()
        block.nonce = 0
        block.hash = block.compute_hash()
        while not block.hash.startswith(self.prefix):
            block.nonce += 1
            block.hash = block.compute_hash()
        logger.info(
            "block %d mined in %.2fs  nonce=%d  hash=%s",
            block.index, time.time() - start, block.nonce, block.hash,
        )
        return block

    def is_valid_proof(self, block: Block) -> bool:
        return block.hash == block.compute_hash() and block.hash.startswith(self.prefix)
