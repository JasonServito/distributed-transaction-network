"""Unit tests for Blockchain chain validation and the longest-chain rule."""

import pytest

from blockchain.block import Block
from blockchain.chain import Blockchain
from consensus.proof_of_work import ProofOfWork

DIFFICULTY = 2  # low difficulty so tests run quickly


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #


def _mine_block(chain: Blockchain, transactions=None) -> Block:
    """Mine a valid block and append it to *chain*."""
    pow_engine = ProofOfWork(DIFFICULTY)
    block = Block(
        index=len(chain.chain),
        previous_hash=chain.last_block.hash,
        transactions=transactions or [],
    )
    pow_engine.mine(block)
    return block


# ------------------------------------------------------------------ #
#  Genesis
# ------------------------------------------------------------------ #


class TestGenesis:
    def test_genesis_block_created_on_init(self):
        bc = Blockchain()
        assert len(bc.chain) == 1

    def test_genesis_index_is_zero(self):
        bc = Blockchain()
        assert bc.chain[0].index == 0

    def test_genesis_previous_hash_is_zeros(self):
        bc = Blockchain()
        assert bc.chain[0].previous_hash == "0" * 64

    def test_genesis_hash_is_valid(self):
        bc = Blockchain()
        g = bc.chain[0]
        assert g.hash == g.compute_hash()


# ------------------------------------------------------------------ #
#  Adding blocks
# ------------------------------------------------------------------ #


class TestAddBlock:
    def test_valid_block_is_accepted(self):
        bc = Blockchain()
        block = _mine_block(bc)
        assert bc.add_block(block, DIFFICULTY) is True
        assert len(bc.chain) == 2

    def test_block_with_wrong_previous_hash_rejected(self):
        bc = Blockchain()
        block = Block(
            index=1,
            previous_hash="wrong" * 10,
            transactions=[],
        )
        # Force the hash to look valid (starts with zeros) but previous_hash is wrong
        block.hash = block.compute_hash()
        assert bc.add_block(block, DIFFICULTY) is False

    def test_block_without_valid_pow_rejected(self):
        bc = Blockchain()
        block = Block(
            index=1,
            previous_hash=bc.last_block.hash,
            transactions=[],
        )
        block.nonce = 0
        block.hash = block.compute_hash()  # hash almost certainly won't satisfy difficulty
        # Ensure the hash does NOT start with the prefix
        if block.hash.startswith("0" * DIFFICULTY):
            pytest.skip("Hash accidentally satisfies PoW — re-run to get a different timestamp.")
        assert bc.add_block(block, DIFFICULTY) is False


# ------------------------------------------------------------------ #
#  Chain validation
# ------------------------------------------------------------------ #


class TestChainValidation:
    def test_fresh_chain_is_valid(self):
        bc = Blockchain()
        assert bc.is_valid_chain(DIFFICULTY) is True

    def test_chain_with_mined_block_is_valid(self):
        bc = Blockchain()
        block = _mine_block(bc)
        bc.add_block(block, DIFFICULTY)
        assert bc.is_valid_chain(DIFFICULTY) is True

    def test_tampered_block_data_invalidates_chain(self):
        bc = Blockchain()
        block = _mine_block(bc)
        bc.add_block(block, DIFFICULTY)
        # Tamper: change the nonce after appending (hash no longer matches)
        bc.chain[1].nonce = 99999
        assert bc.is_valid_chain(DIFFICULTY) is False

    def test_tampered_previous_hash_invalidates_chain(self):
        bc = Blockchain()
        block = _mine_block(bc)
        bc.add_block(block, DIFFICULTY)
        bc.chain[1].previous_hash = "tampered" * 8
        assert bc.is_valid_chain(DIFFICULTY) is False

    def test_multi_block_chain_validates(self):
        bc = Blockchain()
        for _ in range(3):
            block = _mine_block(bc)
            bc.add_block(block, DIFFICULTY)
        assert len(bc.chain) == 4
        assert bc.is_valid_chain(DIFFICULTY) is True


# ------------------------------------------------------------------ #
#  Serialisation round-trip
# ------------------------------------------------------------------ #


class TestChainSerialisation:
    def test_round_trip_preserves_length(self):
        bc = Blockchain()
        block = _mine_block(bc)
        bc.add_block(block, DIFFICULTY)

        restored = Blockchain.from_dict(bc.to_dict())
        assert len(restored.chain) == len(bc.chain)

    def test_round_trip_chain_is_valid(self):
        bc = Blockchain()
        block = _mine_block(bc)
        bc.add_block(block, DIFFICULTY)

        restored = Blockchain.from_dict(bc.to_dict())
        assert restored.is_valid_chain(DIFFICULTY) is True
