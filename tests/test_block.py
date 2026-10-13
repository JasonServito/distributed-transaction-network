"""Unit tests for Block hashing and serialisation."""
import pytest

from blockchain.block import Block
from blockchain.transaction import Transaction


# ------------------------------------------------------------------ #
#  Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def simple_block():
    """A block with no transactions, nonce=0."""
    b = Block(index=1, previous_hash="a" * 64, transactions=[])
    b.hash = b.compute_hash()
    return b


@pytest.fixture
def coinbase_tx():
    return Transaction(sender="COINBASE", receiver="miner", amount=10.0, timestamp=1000.0)


# ------------------------------------------------------------------ #
#  Hash tests
# ------------------------------------------------------------------ #

class TestBlockHashing:
    def test_compute_hash_returns_64_hex_chars(self, simple_block):
        h = simple_block.compute_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_changes_when_nonce_changes(self, simple_block):
        h1 = simple_block.compute_hash()
        simple_block.nonce += 1
        h2 = simple_block.compute_hash()
        assert h1 != h2

    def test_hash_changes_when_previous_hash_changes(self, simple_block):
        h1 = simple_block.compute_hash()
        simple_block.previous_hash = "b" * 64
        h2 = simple_block.compute_hash()
        assert h1 != h2

    def test_hash_is_deterministic(self, simple_block):
        """Same block data must always produce the same hash."""
        h1 = simple_block.compute_hash()
        h2 = simple_block.compute_hash()
        assert h1 == h2

    def test_stored_hash_matches_computed(self, simple_block):
        assert simple_block.hash == simple_block.compute_hash()

    def test_tampered_block_hash_mismatch(self, simple_block):
        """Changing a field after setting hash invalidates stored hash."""
        simple_block.nonce = 999
        assert simple_block.hash != simple_block.compute_hash()

    def test_hash_includes_transactions(self, coinbase_tx):
        b_without = Block(index=1, previous_hash="a" * 64, transactions=[])
        b_with = Block(index=1, previous_hash="a" * 64, transactions=[coinbase_tx])
        b_without.timestamp = b_with.timestamp  # force same timestamp
        assert b_without.compute_hash() != b_with.compute_hash()


# ------------------------------------------------------------------ #
#  Serialisation round-trip
# ------------------------------------------------------------------ #

class TestBlockSerialisation:
    def test_to_dict_contains_required_keys(self, simple_block):
        d = simple_block.to_dict()
        for key in ("index", "previous_hash", "transactions", "timestamp", "nonce", "hash"):
            assert key in d

    def test_round_trip(self, simple_block):
        d = simple_block.to_dict()
        restored = Block.from_dict(d)
        assert restored.index == simple_block.index
        assert restored.previous_hash == simple_block.previous_hash
        assert restored.nonce == simple_block.nonce
        assert restored.hash == simple_block.hash

    def test_round_trip_preserves_hash_validity(self, simple_block):
        restored = Block.from_dict(simple_block.to_dict())
        assert restored.hash == restored.compute_hash()
