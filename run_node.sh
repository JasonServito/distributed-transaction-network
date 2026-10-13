#!/usr/bin/env bash
# run_node.sh — launch a single blockchain node.
#
# Usage:
#   ./run_node.sh [PORT] [SEED_PEER]
#
# Examples:
#   ./run_node.sh 5000              # bootstrap node
#   ./run_node.sh 5001 127.0.0.1:5000  # node that seeds from port 5000
#   ./run_node.sh 5002 127.0.0.1:5000

set -euo pipefail

PORT="${1:-5000}"
SEED="${2:-}"

echo "==> Starting node on port $PORT  seed='${SEED:-none}'"

if [ -n "$SEED" ]; then
    python3 main.py "$PORT" "$SEED"
else
    python3 main.py "$PORT"
fi
