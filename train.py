"""
Autoresearch experiment loop driver for linux-vm-autoresearch.
This script is meant to be read by the LLM agent (via program.md) and
serves as documentation of the loop. The actual loop is driven by the
LLM agent itself following the instructions in program.md.

This file can also be run standalone to execute a single experiment cycle
for testing purposes.

Usage:
    python train.py              # run one experiment cycle (for testing)
    python train.py --baseline   # run baseline only
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
V86_DIR = PROJECT_DIR / "v86-workdir"
JIT_RS = V86_DIR / "src" / "rust" / "jit.rs"


def run_baseline():
    """Run emulate.py with unmodified jit.rs to establish baseline."""
    print("=" * 60)
    print("BASELINE RUN")
    print("=" * 60)

    result = subprocess.run(
        ["python", str(PROJECT_DIR / "emulate.py")],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=300,
    )

    print(result.stdout)
    if result.returncode != 0:
        print("Baseline run failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    # Save as run.log
    log_path = PROJECT_DIR / "run.log"
    log_path.write_text(result.stdout)

    # Record in evo_db
    # Get current commit hash
    git_result = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        capture_output=True, text=True, cwd=str(PROJECT_DIR),
    )
    commit = git_result.stdout.strip() if git_result.returncode == 0 else "0000000"

    subprocess.run([
        "python", str(PROJECT_DIR / "evo_db.py"), "add",
        "--commit", commit,
        "--description", "baseline: unmodified jit.rs",
        "--log", str(log_path),
    ], cwd=str(PROJECT_DIR))

    print("\nBaseline recorded. Ready for experimentation.")


def run_single_cycle():
    """Run a single experiment cycle (for testing the pipeline)."""
    print("=" * 60)
    print("SINGLE EXPERIMENT CYCLE (test mode)")
    print("=" * 60)

    # Sample from evo_db
    result = subprocess.run(
        ["python", str(PROJECT_DIR / "evo_db.py"), "sample"],
        capture_output=True, text=True, cwd=str(PROJECT_DIR),
    )

    if result.returncode != 0:
        print("Sample failed:", result.stderr, file=sys.stderr)
        sys.exit(1)

    sample = json.loads(result.stdout)
    print(f"Strategy: {sample['strategy']}")
    print(f"Suggestion: {sample['suggestion']}")

    if sample["parent"] is None:
        print("\nNo experiments yet. Running baseline first...")
        run_baseline()
        return

    print(f"\nParent: {sample['parent']['commit']} - {sample['parent']['description']}")
    print("\nIn autonomous mode, the LLM agent would now:")
    print("  1. Restore parent's jit.rs")
    print("  2. Design and apply a modification")
    print("  3. Commit the change")
    print("  4. Run emulate.py")
    print("  5. Record results in evo_db")
    print("\nRun this with an LLM agent following program.md for full automation.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experiment loop driver")
    parser.add_argument("--baseline", action="store_true", help="Run baseline only")
    args = parser.parse_args()

    if not JIT_RS.exists():
        print(f"ERROR: {JIT_RS} not found. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)

    if args.baseline:
        run_baseline()
    else:
        run_single_cycle()
