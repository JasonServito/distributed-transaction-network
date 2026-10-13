import hashlib
import json


def sha256(data: str) -> str:
    """Return the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def hash_dict(d: dict) -> str:
    """Deterministically SHA-256 hash a dictionary (sorted keys)."""
    serialized = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return sha256(serialized)
