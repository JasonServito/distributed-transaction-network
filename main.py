"""
Entry point — spin up a single blockchain node.

Usage
-----
    python main.py [PORT] [SEED_PEER]

Examples
--------
    # Node 1 — no seed
    python main.py 5000

    # Node 2 — seeds from node 1
    python main.py 5001 127.0.0.1:5000

    # Node 3 — seeds from node 1
    python main.py 5002 127.0.0.1:5000

Environment variables (see config.py or .env) override defaults.
CLI arguments take the highest precedence.
"""
import sys

import config
from network.node import create_app
from utils.logger import get_logger

logger = get_logger("main")


def main() -> None:
    # ------------------------------------------------------------------ #
    #  Parse optional CLI overrides
    # ------------------------------------------------------------------ #
    if len(sys.argv) > 1:
        try:
            config.PORT = int(sys.argv[1])
        except ValueError:
            logger.error("Invalid PORT argument '%s'. Must be an integer.", sys.argv[1])
            sys.exit(1)

    if len(sys.argv) > 2:
        config.SEED_PEER = sys.argv[2].strip()

    # ------------------------------------------------------------------ #
    #  Start Flask server
    # ------------------------------------------------------------------ #
    logger.info(
        "Starting node  port=%d  difficulty=%d  seed='%s'  miner='%s'",
        config.PORT,
        config.DIFFICULTY,
        config.SEED_PEER or "(none)",
        config.MINER_ADDRESS,
    )

    app = create_app(config)
    # debug=False is important for production-like behaviour; use
    # FLASK_DEBUG=1 env var if you want the Werkzeug reloader.
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
