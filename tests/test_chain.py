"""Unit tests for Blockchain chain validation and the longest-chain rule."""

import pytest

from blockchain.block import Block
from blockchain.chain import Blockchain
from blockchain.transaction import Transaction
from consensus.proof_of_work import ProofOfWork
from utils.signing import generate_keypair, public_key_to_pem

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
        transactions=transactions or [_coinbase()],
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


@pytest.fixture(scope="module")
def wallet():
    """A key pair: (private key, address)."""
    private_key, public_key = generate_keypair()
    return private_key, public_key_to_pem(public_key)


def _signed(wallet, amount: float, receiver: str = "someone") -> Transaction:
    private_key, address = wallet
    tx = Transaction(sender=address, receiver=receiver, amount=amount)
    tx.sign(private_key)
    return tx


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
            transactions=[_coinbase()],
        )
        # Valid PoW and coinbase, so the unknown parent is the only problem
        ProofOfWork(DIFFICULTY).mine(block)
        assert bc.add_block(block, DIFFICULTY) is False

    def test_block_without_valid_pow_rejected(self):
        bc = Blockchain()
        block = Block(
            index=1,
            previous_hash=bc.last_block.hash,
            transactions=[_coinbase()],
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
#  Coinbase rules (B-14)
# ------------------------------------------------------------------ #


class TestCoinbase:
    def test_block_without_a_coinbase_rejected(self, wallet):
        bc = Blockchain()
        genesis = bc.last_block
        for transactions in ([], [_signed(wallet, 1.0)]):
            assert bc.add_block(_block_on(genesis, transactions), DIFFICULTY) is False

    def test_block_with_two_coinbases_rejected(self):
        bc = Blockchain()
        block = _block_on(bc.last_block, [_coinbase(), _coinbase()])
        assert bc.add_block(block, DIFFICULTY) is False

    def test_coinbase_not_first_rejected(self, wallet):
        bc = Blockchain()
        block = _block_on(bc.last_block, [_signed(wallet, 1.0), _coinbase()])
        assert bc.add_block(block, DIFFICULTY) is False

    def test_coinbase_must_pay_exactly_the_reward(self):
        bc = Blockchain(mining_reward=10.0)
        genesis = bc.last_block
        for amount in (9.0, 10.5, 1000.0):
            assert bc.add_block(_block_on(genesis, [_coinbase(amount=amount)]), DIFFICULTY) is False
        assert bc.add_block(_block_on(genesis, [_coinbase(amount=10.0)]), DIFFICULTY) is True

    def test_coinbase_amount_checked_after_a_dict_round_trip(self):
        bc = Blockchain(mining_reward=10)  # from_dict turns every amount into a float
        genesis = bc.last_block
        wrong = Block.from_dict(_block_on(genesis, [_coinbase(amount=11.0)]).to_dict())
        assert bc.add_block(wrong, DIFFICULTY) is False
        right = Block.from_dict(_block_on(genesis, [_coinbase(amount=10.0)]).to_dict())
        assert bc.add_block(right, DIFFICULTY) is True


# ------------------------------------------------------------------ #
#  Balances inside a block (B-15)
# ------------------------------------------------------------------ #


def _funded_chain(wallet):
    """A chain whose block 1 pays the 10-coin reward to *wallet*; returns (chain, block 1)."""
    bc = Blockchain()
    funding = _block_on(bc.last_block, [_coinbase(receiver=wallet[1])])
    assert bc.add_block(funding, DIFFICULTY)
    return bc, funding


class TestBalances:
    def test_overspend_rejected(self, wallet):
        bc, funding = _funded_chain(wallet)
        assert bc.add_block(_block_on(funding, [_coinbase(), _signed(wallet, 10.5)]), DIFFICULTY) is False
        assert bc.add_block(_block_on(funding, [_coinbase(), _signed(wallet, 10.0)]), DIFFICULTY) is True

    def test_txs_that_fit_alone_but_overspend_together_rejected(self, wallet):
        bc, funding = _funded_chain(wallet)
        block = _block_on(funding, [_coinbase(), _signed(wallet, 6.0), _signed(wallet, 6.0)])
        assert bc.add_block(block, DIFFICULTY) is False

    def test_miner_can_spend_its_reward_in_the_same_block(self, wallet):
        bc = Blockchain()
        block = _block_on(bc.last_block, [_coinbase(receiver=wallet[1]), _signed(wallet, 10.0)])
        assert bc.add_block(block, DIFFICULTY) is True

    def test_balances_come_from_the_branch_the_block_extends(self, wallet):
        bc = Blockchain()
        genesis = bc.last_block
        m1 = _block_on(genesis)
        m2 = _block_on(m1)
        side = _block_on(genesis, [_coinbase(receiver=wallet[1])])  # only this branch funds the wallet
        for block in (m1, m2, side):
            assert bc.add_block(block, DIFFICULTY)
        assert bc.last_block.hash == m2.hash
        assert bc.get_balance(wallet[1]) == 0.0

        assert bc.add_block(_block_on(side, [_coinbase(), _signed(wallet, 5.0)]), DIFFICULTY) is True
        assert bc.add_block(_block_on(m2, [_coinbase(), _signed(wallet, 5.0)]), DIFFICULTY) is False


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
