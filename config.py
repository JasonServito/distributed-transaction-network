import os
from dotenv import load_dotenv

load_dotenv()

# Mining difficulty — number of leading zeros required in block hash
DIFFICULTY: int = int(os.getenv("DIFFICULTY", "3"))

# HTTP server binding
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "5000"))
# Address this node advertises to peers when registering itself.
# Override to the container hostname when running inside Docker.
HOST_ADDR: str = os.getenv("HOST_ADDR", "127.0.0.1")

# Optional address of a known peer to bootstrap from
SEED_PEER: str = os.getenv("SEED_PEER", "")

# Coinbase reward paid to the miner of each block
MINING_REWARD: float = float(os.getenv("MINING_REWARD", "10.0"))

# Address (public key PEM or label) that receives the mining reward
MINER_ADDRESS: str = os.getenv("MINER_ADDRESS", "miner_default")
