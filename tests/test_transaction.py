"""Unit tests for Transaction signing, verification, and validation."""

import time

import pytest

from blockchain.transaction import Transaction
from utils.signing import generate_keypair, public_key_to_pem

# ------------------------------------------------------------------ #
#  Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def keypair():
    private_key, public_key = generate_keypair()
    return private_key, public_key


@pytest.fixture(scope="module")
def sender_pem(keypair):
    _, pub = keypair
    return public_key_to_pem(pub)


@pytest.fixture
def signed_tx(keypair, sender_pem):
    priv, _ = keypair
    tx = Transaction(
        sender=sender_pem,
        receiver="receiver_address",
        amount=5.0,
        timestamp=time.time(),
    )
    tx.sign(priv)
    return tx


# ------------------------------------------------------------------ #
#  Coinbase transactions
# ------------------------------------------------------------------ #


class TestCoinbaseTransaction:
    def test_coinbase_is_valid_without_signature(self):
        tx = Transaction(sender="COINBASE", receiver="miner", amount=10.0)
        assert tx.is_valid() is True

    def test_coinbase_with_zero_amount_is_invalid(self):
        tx = Transaction(sender="COINBASE", receiver="miner", amount=0.0)
        assert tx.is_valid() is False

    def test_coinbase_with_negative_amount_is_invalid(self):
        tx = Transaction(sender="COINBASE", receiver="miner", amount=-1.0)
        assert tx.is_valid() is False


# ------------------------------------------------------------------ #
#  Signed transactions
# ------------------------------------------------------------------ #


class TestSignedTransaction:
    def test_valid_signed_tx_passes_validation(self, signed_tx):
        assert signed_tx.is_valid() is True

    def test_unsigned_tx_fails_validation(self, sender_pem):
        tx = Transaction(sender=sender_pem, receiver="recv", amount=1.0)
        assert tx.is_valid() is False

    def test_tampered_amount_invalidates_signature(self, signed_tx):
        signed_tx.amount = 9999.0  # tamper
        assert signed_tx.is_valid() is False

    def test_tampered_receiver_invalidates_signature(self, signed_tx, keypair, sender_pem):
        priv, _ = keypair
        tx = Transaction(sender=sender_pem, receiver="original", amount=1.0, timestamp=1000.0)
        tx.sign(priv)
        tx.receiver = "attacker"  # tamper after signing
        assert tx.is_valid() is False

    def test_wrong_signature_fails(self, sender_pem):
        _, other_pub = generate_keypair()
        other_priv, _ = generate_keypair()
        tx = Transaction(sender=sender_pem, receiver="recv", amount=1.0, timestamp=1000.0)
        tx.sign(other_priv)  # signed with a different key
        assert tx.is_valid() is False

    def test_to_dict_round_trip_preserves_validity(self, signed_tx):
        restored = Transaction.from_dict(signed_tx.to_dict())
        assert restored.is_valid() is True


# ------------------------------------------------------------------ #
#  Serialisation
# ------------------------------------------------------------------ #


class TestTransactionSerialisation:
    def test_to_dict_has_required_keys(self, signed_tx):
        d = signed_tx.to_dict()
        for key in ("tx_id", "sender", "receiver", "amount", "timestamp", "signature"):
            assert key in d

    def test_from_dict_restores_all_fields(self, signed_tx):
        d = signed_tx.to_dict()
        restored = Transaction.from_dict(d)
        assert restored.tx_id == signed_tx.tx_id
        assert restored.sender == signed_tx.sender
        assert restored.receiver == signed_tx.receiver
        assert restored.amount == signed_tx.amount
        assert restored.signature == signed_tx.signature
