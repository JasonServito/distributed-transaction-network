"""
Minimal wallet: make a key pair, send a signed payment, check a balance.

Usage
-----
    python wallet.py keygen alice            # writes wallets/alice.key and wallets/alice.pub
    python wallet.py send --key wallets/alice.key --to wallets/bob.pub --amount 3 --node 127.0.0.1:5000
    python wallet.py balance --pub wallets/alice.pub --node 127.0.0.1:5000

An address is the PEM text of a public key without surrounding whitespace (what
a .pub file holds), or a plain label such as "miner_default".  The .key file is
the private key: anyone who has it can spend the wallet's coins, so keep it secret.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import requests

from blockchain.transaction import Transaction
from utils.signing import generate_keypair, pem_to_private_key, private_key_to_pem, public_key_to_pem

DEFAULT_NODE = "127.0.0.1:5000"
TIMEOUT = 5  # seconds per request to the node


class WalletError(Exception):
    """The node refused a request; the message is the node's error."""


def keygen(name: str, directory: str = "wallets") -> Tuple[Path, Path]:
    """Write <directory>/<name>.key (private key) and <name>.pub (public key: the address)."""
    key_path = Path(directory) / f"{name}.key"
    pub_path = Path(directory) / f"{name}.pub"
    for path in (key_path, pub_path):
        if path.exists():
            raise FileExistsError(f"{path} already exists; not overwriting it")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key, public_key = generate_keypair()
    key_path.touch(mode=0o600, exist_ok=False)  # owner-only (on POSIX) before the key goes in
    key_path.write_text(private_key_to_pem(private_key))
    pub_path.write_text(public_key_to_pem(public_key))
    return key_path, pub_path


def read_address(pub_file: str) -> str:
    """The address held in a .pub file."""
    return Path(pub_file).read_text().strip()


def send(key_file: str, to: str, amount: float, node: str = DEFAULT_NODE) -> str:
    """
    Sign a payment of *amount* from the wallet in *key_file* and submit it to *node*.
    *to* is a .pub file (which must exist) or a plain label.  Returns the tx_id.
    """
    private_key = pem_to_private_key(Path(key_file).read_text())
    sender = public_key_to_pem(private_key.public_key()).strip()
    receiver = read_address(to) if to.endswith(".pub") else to.strip()
    # float(): the node reads the amount as a float, so sign it as one ("4.0", not "4").
    tx = Transaction(sender=sender, receiver=receiver, amount=float(amount))
    tx.sign(private_key)
    reply = _reply(requests.post(f"http://{node}/transactions/new", json=tx.to_dict(), timeout=TIMEOUT))
    return reply["tx_id"]


def balance(address: str, node: str = DEFAULT_NODE) -> float:
    """Confirmed (on-chain) balance of *address*, as *node* sees it."""
    reply = _reply(requests.get(f"http://{node}/balance", params={"address": address}, timeout=TIMEOUT))
    return reply["balance"]


def _reply(resp) -> dict:
    """The node's JSON reply; raises WalletError with the node's error if it refused."""
    try:
        body = resp.json()
    except ValueError:  # not JSON, e.g. an HTML error page
        body = None
    body = body if isinstance(body, dict) else {}
    if resp.status_code >= 400:
        error = body.get("error", f"HTTP {resp.status_code}")
        details = ", ".join(f"{k}={v}" for k, v in body.items() if k != "error")
        raise WalletError(f"{error} ({details})" if details else error)
    return body


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wallet.py", description="Minimal wallet: make keys, send a signed payment, check a balance."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    node_help = f"node host:port (default: {DEFAULT_NODE})"

    keygen_cmd = commands.add_parser("keygen", help="create NAME.key (private key) and NAME.pub (address)")
    keygen_cmd.add_argument("name")
    keygen_cmd.add_argument("--dir", default="wallets", help="folder for the key files (default: wallets)")

    send_cmd = commands.add_parser("send", help="sign a payment and submit it to a node")
    send_cmd.add_argument("--key", required=True, help="sender's private key file (.key)")
    send_cmd.add_argument("--to", required=True, help="receiver: a .pub file, or a plain label")
    send_cmd.add_argument("--amount", required=True, type=float)
    send_cmd.add_argument("--node", default=DEFAULT_NODE, help=node_help)

    balance_cmd = commands.add_parser("balance", help="print an address's confirmed balance")
    who = balance_cmd.add_mutually_exclusive_group(required=True)
    who.add_argument("--pub", help="the wallet's .pub file")
    who.add_argument("--address", help="a plain label, e.g. miner_default")
    balance_cmd.add_argument("--node", default=DEFAULT_NODE, help=node_help)

    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            key_path, pub_path = keygen(args.name, args.dir)
            print(f"{key_path}  private key: keep it secret")
            print(f"{pub_path}  public key: your address")
        elif args.command == "send":
            print(send(args.key, args.to, args.amount, args.node))
        else:
            address = read_address(args.pub) if args.pub else args.address
            print(balance(address, args.node))
    except (WalletError, OSError, ValueError, requests.RequestException) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
