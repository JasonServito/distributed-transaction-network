import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate_keypair() -> tuple:
    private_key = ec.generate_private_key(ec.SECP256K1(), default_backend())
    return private_key, private_key.public_key()


def sign(private_key, message: str) -> str:
    raw = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(raw).decode()


def verify(public_key, message: str, signature_b64: str) -> bool:
    try:
        public_key.verify(base64.b64decode(signature_b64), message.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, Exception):
        return False


def public_key_to_pem(public_key) -> str:
    return public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def pem_to_public_key(pem: str):
    return serialization.load_pem_public_key(pem.encode(), backend=default_backend())


def private_key_to_pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def pem_to_private_key(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())
