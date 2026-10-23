"""Unit tests for Blockchain chain validation and the longest-chain rule."""

import pytest

from blockchain.block import Block
from blockchain.chain import Blockchain
from blockchain.transaction import Transaction
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


def _coinbase(receiver: str = "miner", amount: float = 10.0) -> Transaction:
    return Transaction(sender="COINBASE", receiver=receiver, amount=amount)


def _block_on(parent: Block, transactions=None, index=None) -> Block:
    """Mine a block on *parent*: by default the next index, holding just a coinbase."""
    block = Block(
        index=parent.index + 1 if index is None else index,
        previous_hash=parent.hash,
        transactions=[_coinbase()] if transactions is None else transactions,
    )
    return ProofOfWork(DIFFICULTY).mine(block)


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
#  Block index (B-13)
# ------------------------------------------------------------------ #


class TestBlockIndex:
    def test_block_index_must_follow_its_parent(self):
        bc = Blockchain()
        genesis = bc.last_block
        for wrong_index in (0, 2):
            assert bc.add_block(_block_on(genesis, index=wrong_index), DIFFICULTY) is False
        assert bc.add_block(_block_on(genesis), DIFFICULTY) is True

    def test_index_rule_holds_on_a_side_branch(self):
        bc = Blockchain()
        m1 = _block_on(bc.last_block)
        m2 = _block_on(m1)
        for block in (m1, m2):
            assert bc.add_block(block, DIFFICULTY)

        # A block on m1 follows m1's index, not the main chain tip's.
        assert bc.add_block(_block_on(m1, index=m2.index + 1), DIFFICULTY) is False
        side = _block_on(m1)
        assert bc.add_block(side, DIFFICULTY) is True
        assert bc.last_block.hash == m2.hash


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
