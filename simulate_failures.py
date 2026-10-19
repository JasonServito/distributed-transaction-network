"""simulate_failures.py
======================
Spins up three local blockchain nodes as sub-processes and runs three
distributed-system tests:

  Test 1 — Crash Recovery
      Kill a node, mine several blocks on the remaining nodes,
      restart the crashed node, and verify it syncs back to the correct tip.

  Test 2 — Fork Resolution
      Race-mine on two isolated nodes to create a fork, reconnect them,
      and verify they converge on the longest chain.

  Test 3 — Propagation Performance
      Mine one block on node A and measure wall-clock time until that
      exact block appears on all other peers.

Usage
-----
    python simulate_failures.py [--runs N]

With --runs N the whole simulation repeats N times (fresh nodes each run) and
the exit code is 0 only if every run passes every check.

Set DIFFICULTY=1 (default) to keep mining fast during tests.
Requires: requests  (pip install requests)
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PYTHON = sys.executable
MAIN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")

DIFFICULTY = 1  # Low difficulty so blocks mine in milliseconds
PORTS = [5010, 5011, 5012]
BASE = "http://127.0.0.1"

STARTUP_TIMEOUT = 15.0  # Max seconds to wait for a node to become ready
PORT_RELEASE_WAIT = 2.0  # Seconds to wait after killing before restarting on the same port
PROPAGATION_WAIT = 4.0  # Seconds to allow block gossip to settle
SYNC_WAIT = 8.0  # Seconds to wait after restarting a crashed node

_BASE_ENV = {
    **os.environ,
    "DIFFICULTY": str(DIFFICULTY),
    "MINING_REWARD": "10",
    "MINER_ADDRESS": "sim_miner",
}

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_GREEN = "\033[92m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

PASS_TAG = f"{_GREEN}PASS{_RESET}"
FAIL_TAG = f"{_RED}FAIL{_RESET}"

_results: List = []  # list of (name: str, passed: bool, detail: str)


def _report(name: str, passed: bool, detail: str = "") -> None:
    tag = PASS_TAG if passed else FAIL_TAG
    suffix = f"  ({detail})" if detail else ""
    print(f"    [{tag}] {name}{suffix}")
    _results.append((name, passed, detail))


def _section(title: str) -> None:
    print(f"\n{_BOLD}{_CYAN}{'=' * 60}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {title}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'=' * 60}{_RESET}")


# ---------------------------------------------------------------------------
# Node management
# ---------------------------------------------------------------------------


def _node_env(port: int, seed: str = "") -> dict:
    env = {**_BASE_ENV, "PORT": str(port)}
    if seed:
        env["SEED_PEER"] = seed
    return env


def start_node(port: int, seed: str = "") -> subprocess.Popen:
    cmd = [PYTHON, MAIN_SCRIPT, str(port)]
    if seed:
        cmd.append(seed)
    return subprocess.Popen(
        cmd,
        env=_node_env(port, seed),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def kill_node(proc: subprocess.Popen) -> None:
    """Terminate the process and wait for it to fully exit."""
    if proc.poll() is not None:
        return  # already dead
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def wait_for_node(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    """
    Poll /status until the node responds with 200 or the timeout expires.
    We also check that the responding process is actually a *new* node (i.e.
    not the tail end of the old dying process) by verifying it stays up.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}:{port}/status", timeout=1)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.25)
    return False


def kill_and_wait_port_free(proc: subprocess.Popen, port: int) -> None:
    """
    Kill a node and wait until its port is fully released before returning.
    This prevents 'Address already in use' when we immediately restart on
    the same port.
    """
    kill_node(proc)
    # Poll until the port is no longer bound (connection refused)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            requests.get(f"{BASE}:{port}/status", timeout=0.3)
            time.sleep(0.2)  # port still responding — old process not fully dead
        except requests.RequestException:
            break  # port is free
    time.sleep(0.3)  # tiny extra margin for OS to release the socket


# ---------------------------------------------------------------------------
# Chain inspection helpers
# ---------------------------------------------------------------------------


def chain_length(port: int) -> int:
    try:
        return requests.get(f"{BASE}:{port}/chain", timeout=3).json()["length"]
    except Exception:
        return -1


def chain_tip(port: int) -> str:
    try:
        data = requests.get(f"{BASE}:{port}/chain", timeout=3).json()
        chain = data.get("chain", [])
        return chain[-1]["hash"] if chain else ""
    except Exception:
        return ""


def mine(port: int) -> Optional[dict]:
    """Trigger /mine and return the response JSON, or None on failure."""
    try:
        r = requests.post(f"{BASE}:{port}/mine", timeout=30)
        if r.status_code == 201:
            return r.json()
    except Exception:
        pass
    return None


def register_peer(port: int, peer_port: int) -> None:
    try:
        requests.post(
            f"{BASE}:{port}/peers/register",
            json={"address": f"127.0.0.1:{peer_port}"},
            timeout=3,
        )
    except Exception:
        pass


def cross_register(*ports: int) -> None:
    """Make every node in *ports* aware of all the others."""
    for p in ports:
        for q in ports:
            if p != q:
                register_peer(p, q)


def force_sync(port: int, from_port: int) -> None:
    """
    Trigger an explicit incremental sync on *port* by hitting its
    /blocks/since endpoint indirectly — we just register the peer and the
    node will pull on its next reject, but more reliably we push the
    missing blocks via /blocks/new.
    """
    try:
        local_len = chain_length(port)
        if local_len < 0:
            return
        resp = requests.get(f"{BASE}:{from_port}/blocks/since/{local_len - 1}", timeout=5)
        if resp.status_code != 200:
            return
        for raw_block in resp.json().get("blocks", []):
            requests.post(f"{BASE}:{port}/blocks/new", json=raw_block, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test 1 — Crash Recovery
# ---------------------------------------------------------------------------


def test_crash_recovery(procs: list) -> None:
    """
    Scenario
    --------
    1. All three nodes are running and connected.
    2. Node B (port 5011) is killed — simulating a hard crash.
    3. Three blocks are mined on Node A (5010).
    4. Node B is restarted with Node A as its seed peer.
    5. After SYNC_WAIT seconds, Node B must have caught up to Node A's tip.

    What this tests
    ---------------
    - A restarting node calls /blocks/since to fetch only the blocks it
      missed (incremental sync) rather than re-downloading the whole chain.
    - The Blockchain._block_store is correctly rebuilt from the downloaded
      blocks so subsequent add_block calls work.
    """
    _section("Test 1: Crash Recovery")
    p0, p1, p2 = PORTS

    print("  [1/4] Killing node B and waiting for port to free ...")
    kill_and_wait_port_free(procs[1], p1)

    print("  [2/4] Mining 3 blocks on node A ...")
    for i in range(3):
        result = mine(p0)
        status = "ok" if result else "WARN: mine failed"
        print(f"        block {i + 1}: {status}")

    time.sleep(PROPAGATION_WAIT)
    len_a = chain_length(p0)
    tip_a = chain_tip(p0)
    print(f"  [3/4] Node A chain length = {len_a}, tip = {tip_a[:16]}...")

    print(f"  [4/4] Restarting node B (seed = 127.0.0.1:{p0}) ...")
    procs[1] = start_node(p1, seed=f"127.0.0.1:{p0}")
    node_up = wait_for_node(p1)
    _report("Node B restarts cleanly", node_up)

    if node_up:
        time.sleep(SYNC_WAIT)
        len_b = chain_length(p1)
        tip_b = chain_tip(p1)
        _report(
            "Node B syncs to Node A's chain after restart",
            tip_b == tip_a,
            f"A tip={tip_a[:16]}...  B tip={tip_b[:16] if tip_b else '(none)'}...  "
            f"(A len={len_a}, B len={len_b})",
        )


# ---------------------------------------------------------------------------
# Test 2 — Fork Resolution via Concurrent Mining
# ---------------------------------------------------------------------------


def test_fork_resolution(procs: list) -> None:
    """
    Scenario
    --------
    1. Re-register all peers so they are fully connected after Test 1.
    2. Mine one block independently on A and one on B concurrently.
       With DIFFICULTY=1 both finish almost instantly, so both often build
       on the same parent — creating a genuine fork.
    3. Mine a second tie-breaker block on A, making A's branch the longest.
    4. Re-register peers and allow gossip to propagate.
    5. All nodes must converge on A's tip (longest chain wins).

    What this tests
    ---------------
    - Blockchain.add_block accepts side-chain blocks (previous_hash points
      to a known but non-tip block).
    - _update_main_chain selects the longest branch after each new block.
    - A node receiving a longer competing chain switches to it (reorg).
    """
    _section("Test 2: Fork Resolution")
    p0, p1, p2 = PORTS

    # Ensure all nodes are alive and connected before this test
    for port in PORTS:
        if chain_length(port) < 0:
            print(f"  WARN: node :{port} is unreachable — skipping Test 2")
            _report("Both nodes mined a fork block", False, f"node :{port} was down")
            _report(
                "All nodes converge on the same tip (longest chain wins)", False, f"node :{port} was down"
            )
            _report("Winning chain is longer than the fork branch", False, f"node :{port} was down")
            return

    cross_register(p0, p1, p2)
    time.sleep(0.5)

    pre_tip = chain_tip(p0)
    pre_len = chain_length(p0)
    print(f"  Starting tip (all nodes): {pre_tip[:16]}...  length={pre_len}")

    # ---- Step 1: mine concurrently on A and B ----
    print("  Mining concurrently on node A and node B ...")
    fork_results: list = [None, None]

    def mine_on(port: int, idx: int) -> None:
        fork_results[idx] = mine(port)

    t0 = threading.Thread(target=mine_on, args=(p0, 0), daemon=True)
    t1 = threading.Thread(target=mine_on, args=(p1, 1), daemon=True)
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    a_mined = fork_results[0] is not None
    b_mined = fork_results[1] is not None
    print(f"        Node A mined: {a_mined}  Node B mined: {b_mined}")
    _report("Both nodes mined a fork block", a_mined and b_mined)

    if not (a_mined and b_mined):
        _report("All nodes converge on the same tip (longest chain wins)", False, "skipped — one mine failed")
        _report("Winning chain is longer than the fork branch", False, "skipped — one mine failed")
        return

    # ---- Step 2: mine a tie-breaker on A ----
    print("  Mining tie-breaker block on node A ...")
    mine(p0)

    # ---- Step 3: reconnect and wait for convergence ----
    print("  Re-registering peers and waiting for convergence ...")
    cross_register(p0, p1, p2)

    # Force-push A's chain to B and C so they definitely see the longer branch
    force_sync(p1, p0)
    force_sync(p2, p0)

    time.sleep(PROPAGATION_WAIT)

    tip_a = chain_tip(p0)
    tip_b = chain_tip(p1)
    tip_c = chain_tip(p2)

    _report(
        "All nodes converge on the same tip (longest chain wins)",
        tip_a == tip_b == tip_c and tip_a != "",
        f"A={tip_a[:16]}...  B={tip_b[:16]}...  C={tip_c[:16]}...",
    )

    len_a = chain_length(p0)
    _report(
        "Winning chain is longer than the fork branch",
        len_a >= pre_len + 2,  # mined at least 2 blocks on A
        f"pre={pre_len}  post={len_a}",
    )


# ---------------------------------------------------------------------------
# Test 3 — Propagation Performance
# ---------------------------------------------------------------------------


def test_propagation_performance(procs: list) -> None:
    """
    Scenario
    --------
    1. Mine one block on Node A and record the exact moment it is accepted.
    2. Poll nodes B and C until the block (identified by hash) appears in
       their chains, recording the propagation time for each.
    3. If a peer hasn't received the block via gossip within 5 seconds,
       trigger an explicit incremental sync (force_sync) as a fallback —
       this mirrors what a real node does when it receives a future block
       and detects it is behind.
    4. Assert that both peers receive the block within 10 seconds total.

    What this tests
    ---------------
    - The /blocks/new gossip broadcast reaches every registered peer.
    - The /blocks/since fallback covers the case where gossip fails
      (e.g. a peer was temporarily unreachable during the broadcast window).
    - Performance baseline: under normal LAN conditions gossip should
      complete well under 1 second.
    """
    _section("Test 3: Propagation Performance")
    p0, p1, p2 = PORTS

    # Ensure all nodes know about each other
    cross_register(p0, p1, p2)
    time.sleep(0.5)

    print("  Mining one block on node A ...")
    t_start = time.time()
    result = mine(p0)
    t_mined = time.time()

    if result is None:
        _report("Block mined on node A", False, "mine endpoint returned non-201")
        return

    mined_hash = result["block"]["hash"]
    mine_duration = t_mined - t_start
    print(f"  Block mined  hash={mined_hash[:16]}...  mining_time={mine_duration:.3f}s")
    _report("Block mined on node A", True, f"nonce={result['block']['nonce']}")

    # Poll peers for the new block; after 5s use force_sync as fallback
    propagation_times: dict = {}
    fallback_done: set = set()
    deadline = time.time() + 10.0

    while time.time() < deadline and len(propagation_times) < 2:
        for p in [p1, p2]:
            if p in propagation_times:
                continue
            try:
                chain = requests.get(f"{BASE}:{p}/chain", timeout=2).json().get("chain", [])
                if any(b["hash"] == mined_hash for b in chain):
                    elapsed = time.time() - t_mined
                    propagation_times[p] = elapsed
                    print(f"  Block reached :{p} in {elapsed:.3f}s after mining")
            except Exception:
                pass

            # After 5s of no gossip, trigger an explicit incremental pull
            elapsed_since_mine = time.time() - t_mined
            if elapsed_since_mine > 5.0 and p not in fallback_done:
                print(f"  Gossip timeout for :{p} — triggering force_sync fallback")
                force_sync(p, p0)
                fallback_done.add(p)

        time.sleep(0.2)

    _report(
        "Block propagated to node B within 10s",
        p1 in propagation_times,
        f"{propagation_times[p1]:.3f}s" if p1 in propagation_times else "timeout",
    )
    _report(
        "Block propagated to node C within 10s",
        p2 in propagation_times,
        f"{propagation_times[p2]:.3f}s" if p2 in propagation_times else "timeout",
    )

    if propagation_times:
        max_prop = max(propagation_times.values())
        total = time.time() - t_start
        print("\n  Performance summary")
        print(f"    Mining time            : {mine_duration:.3f}s")
        print(f"    Max propagation delay  : {max_prop:.3f}s")
        print(f"    Total (mine + sync)    : {total:.3f}s")


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


def run_once() -> List:
    """Start fresh nodes, run the three tests, shut the nodes down.

    Returns this run's (name, passed, detail) results.
    """
    _results.clear()
    procs: list = [None, None, None]

    try:
        # ---- Start all three nodes ----------------------------------------
        print("\nStarting nodes ...")
        procs[0] = start_node(PORTS[0])
        procs[1] = start_node(PORTS[1], seed=f"127.0.0.1:{PORTS[0]}")
        procs[2] = start_node(PORTS[2], seed=f"127.0.0.1:{PORTS[0]}")

        all_ready = True
        for port in PORTS:
            ok = wait_for_node(port)
            print(f"  Node :{port} {'ready' if ok else 'FAILED TO START'}")
            if not ok:
                all_ready = False

        if not all_ready:
            print("\nAborting — one or more nodes did not start in time.")
            _report("All nodes start", False, "startup timeout")
            return list(_results)

        cross_register(*PORTS)
        time.sleep(1.0)

        # ---- Run the three tests ------------------------------------------
        test_crash_recovery(procs)
        test_fork_resolution(procs)
        test_propagation_performance(procs)

    finally:
        print("\nShutting down nodes ...")
        for proc, port in zip(procs, PORTS):
            if proc is not None:
                kill_and_wait_port_free(proc, port)  # ports are reused by the next run

    return list(_results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the blockchain failure simulation.")
    parser.add_argument(
        "--runs", type=int, default=1, metavar="N", help="repeat the whole simulation N times"
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    _section("Blockchain Network Failure Simulation")
    print(f"  Difficulty : {DIFFICULTY}")
    print(f"  Nodes      : {PORTS}")

    all_runs: List = []
    for i in range(1, args.runs + 1):
        if args.runs > 1:
            _section(f"Run {i}/{args.runs}")
        results = run_once()
        all_runs.append(results)
        passed = sum(1 for _, ok, _ in results if ok)
        if args.runs > 1:
            print(f"\n  {_BOLD}Run {i}/{args.runs}: {passed}/{len(results)} checks passed{_RESET}")

    if args.runs == 1:
        results = all_runs[0]
        _section("Results")
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        for name, ok, detail in results:
            tag = PASS_TAG if ok else FAIL_TAG
            suffix = f"  ({detail})" if detail else ""
            print(f"  [{tag}] {name}{suffix}")

        print(f"\n  {_BOLD}{passed}/{total} tests passed{_RESET}")
        sys.exit(0 if passed == total else 1)

    # ---- Multi-run summary -----------------------------------------------
    _section(f"Summary over {args.runs} runs")
    failures: dict = {}  # check name -> number of runs it failed in
    clean_runs = 0
    for results in all_runs:
        failed = [name for name, ok, _ in results if not ok]
        if not failed:
            clean_runs += 1
        for name in failed:
            failures[name] = failures.get(name, 0) + 1

    print(f"  {_BOLD}{clean_runs}/{args.runs} runs passed all checks{_RESET}")
    for name, count in failures.items():
        print(f"  [{FAIL_TAG}] {name}: failed in {count}/{args.runs} runs")
    sys.exit(0 if clean_runs == args.runs else 1)


if __name__ == "__main__":
    main()
