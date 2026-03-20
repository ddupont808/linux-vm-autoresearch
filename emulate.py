"""
Emulation benchmark for linux-vm-autoresearch.
Builds v86 with the current jit.rs, runs the ISO for a fixed duration,
takes a screenshot, and scores how far into the baseline boot it reached.

Usage:
    python emulate.py                    # run benchmark (default 5s)
    python emulate.py --duration 10      # custom duration
    python emulate.py --iso PATH         # custom ISO
    python emulate.py --skip-build       # skip v86 rebuild (for debugging)

Output:
    Prints a summary block with metrics (parsed by evo_db.py).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent.resolve()
V86_DIR = PROJECT_DIR / "v86-workdir"
BASELINE_DIR = PROJECT_DIR / "baseline"
BASELINE_FRAMES_DIR = BASELINE_DIR / "baseline_frames"
BASELINE_META = BASELINE_DIR / "baseline_meta.json"

DEFAULT_ISO_PATH = PROJECT_DIR / "iso" / "alpine-virt-3.18.4-x86.iso"
EMULATION_DURATION = 5  # seconds — the fixed time budget
V86_BUILD_CMD = ["make", "build/v86.wasm"]

SCREENSHOT_PATH = PROJECT_DIR / "last_screenshot.png"
BENCHMARK_SCRIPT = PROJECT_DIR / "_benchmark_runner.js"

# ---------------------------------------------------------------------------
# Build v86
# ---------------------------------------------------------------------------

def build_v86() -> float:
    """Rebuild v86 with current jit.rs. Returns build time in seconds."""
    t0 = time.time()

    jit_path = V86_DIR / "src" / "rust" / "jit.rs"
    if not jit_path.exists():
        print(f"ERROR: {jit_path} not found. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        V86_BUILD_CMD,
        cwd=str(V86_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    build_time = time.time() - t0

    if result.returncode != 0:
        print(f"BUILD FAILED ({build_time:.1f}s):")
        print(result.stderr)
        sys.exit(1)

    print(f"v86 built in {build_time:.1f}s")
    return build_time


# ---------------------------------------------------------------------------
# Run emulation benchmark
# ---------------------------------------------------------------------------

def run_emulation(iso_path: Path, duration: float) -> dict:
    """
    Run v86 headlessly via Node.js for `duration` seconds.
    Returns dict with metrics.
    """
    _write_benchmark_script(iso_path, duration)

    t0 = time.time()
    result = subprocess.run(
        ["node", str(BENCHMARK_SCRIPT)],
        cwd=str(V86_DIR),
        capture_output=True,
        text=True,
        timeout=int(duration * 3 + 60),
    )
    wall_time = time.time() - t0

    if result.returncode != 0:
        print(f"EMULATION FAILED ({wall_time:.1f}s):")
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        sys.exit(1)

    # Parse JSON metrics from stdout
    try:
        # The benchmark script outputs JSON on the last line
        lines = result.stdout.strip().split("\n")
        metrics_line = lines[-1]
        metrics = json.loads(metrics_line)
    except (json.JSONDecodeError, IndexError):
        print("ERROR: Could not parse benchmark output:", file=sys.stderr)
        print(result.stdout[-1000:], file=sys.stderr)
        sys.exit(1)

    metrics["total_seconds"] = round(wall_time, 1)
    return metrics


def _write_benchmark_script(iso_path: Path, duration: float):
    """Generate the Node.js benchmark runner."""
    iso_str = str(iso_path.resolve()).replace("\\", "/")
    v86_str = str(V86_DIR.resolve()).replace("\\", "/")
    screenshot_str = str(SCREENSHOT_PATH.resolve()).replace("\\", "/")
    duration_ms = int(duration * 1000)

    script = f"""
"use strict";
const fs = require("fs");
const path = require("path");
const V86 = require("{v86_str}/build/libv86.js").V86;

const ISO_PATH = "{iso_str}";
const SCREENSHOT_PATH = "{screenshot_str}";
const DURATION_MS = {duration_ms};

async function main() {{
    const isoBuffer = fs.readFileSync(ISO_PATH);

    let instructionCount = 0;
    let jitCacheHits = 0;

    const emulator = new V86({{
        bios: {{ url: path.join("{v86_str}", "bios/seabios.bin") }},
        vga_bios: {{ url: path.join("{v86_str}", "bios/vgabios.bin") }},
        cdrom: {{ buffer: isoBuffer }},
        memory_size: 256 * 1024 * 1024,
        vga_memory_size: 8 * 1024 * 1024,
        autostart: true,
        screen_dummy: true,
    }});

    const startTime = Date.now();

    // Wait for emulation duration
    await new Promise((resolve) => {{
        const check = setInterval(() => {{
            const elapsed = Date.now() - startTime;
            if (elapsed >= DURATION_MS) {{
                clearInterval(check);
                resolve();
            }}
        }}, 100);

        // Safety timeout
        setTimeout(() => {{
            clearInterval(check);
            resolve();
        }}, DURATION_MS + 5000);
    }});

    // Capture final screenshot
    let screenshotSaved = false;
    try {{
        const screenData = emulator.screen_make_screenshot();
        if (screenData) {{
            fs.writeFileSync(SCREENSHOT_PATH, screenData);
            screenshotSaved = true;
        }}
    }} catch(e) {{}}

    // Gather metrics from emulator state
    const stats = emulator.v86 ? emulator.v86.cpu : {{}};
    const memUsage = process.memoryUsage();

    const metrics = {{
        screenshot_saved: screenshotSaved,
        emulation_duration_ms: DURATION_MS,
        memory_usage_mb: Math.round(memUsage.heapUsed / (1024 * 1024) * 10) / 10,
    }};

    // Output as JSON (last line)
    console.log(JSON.stringify(metrics));

    try {{ emulator.stop(); }} catch(e) {{}}
    process.exit(0);
}}

main().catch(err => {{
    console.error(err);
    process.exit(1);
}});
"""
    BENCHMARK_SCRIPT.write_text(script)


# ---------------------------------------------------------------------------
# Scoring: compare screenshot against baseline frames
# ---------------------------------------------------------------------------

def score_boot_progress(screenshot_path: Path) -> dict:
    """
    Compare the emulation screenshot against the baseline frame sequence.
    Returns the boot progress (0.0 to 1.0) — the fraction of the baseline
    boot that was reached.

    Uses perceptual image hashing (or pixel-level comparison as fallback)
    to find the closest matching baseline frame.
    """
    if not screenshot_path.exists():
        return {"boot_progress": 0.0, "screenshot_match": 0.0, "matched_frame": -1}

    baseline_frames = sorted(BASELINE_FRAMES_DIR.glob("frame_*.png"))
    if not baseline_frames:
        print("WARNING: No baseline frames found. Returning 0 progress.", file=sys.stderr)
        return {"boot_progress": 0.0, "screenshot_match": 0.0, "matched_frame": -1}

    # Try to use PIL for image comparison
    try:
        from PIL import Image
        import struct

        screenshot = Image.open(screenshot_path).convert("L").resize((160, 120))
        screenshot_data = list(screenshot.getdata())

        best_match_idx = -1
        best_similarity = -1.0

        for i, frame_path in enumerate(baseline_frames):
            frame = Image.open(frame_path).convert("L").resize((160, 120))
            frame_data = list(frame.getdata())

            # Normalized cross-correlation
            n = len(screenshot_data)
            mean_s = sum(screenshot_data) / n
            mean_f = sum(frame_data) / n

            num = sum((s - mean_s) * (f - mean_f) for s, f in zip(screenshot_data, frame_data))
            den_s = sum((s - mean_s) ** 2 for s in screenshot_data) ** 0.5
            den_f = sum((f - mean_f) ** 2 for f in frame_data) ** 0.5

            if den_s > 0 and den_f > 0:
                similarity = num / (den_s * den_f)
            else:
                similarity = 1.0 if den_s == 0 and den_f == 0 else 0.0

            if similarity > best_similarity:
                best_similarity = similarity
                best_match_idx = i

        num_frames = len(baseline_frames)
        boot_progress = (best_match_idx + 1) / num_frames if best_match_idx >= 0 else 0.0

        return {
            "boot_progress": round(boot_progress, 4),
            "screenshot_match": round(max(0, best_similarity), 4),
            "matched_frame": best_match_idx,
        }

    except ImportError:
        # Fallback: hash-based comparison
        return _score_hash_based(screenshot_path, baseline_frames)


def _score_hash_based(screenshot_path: Path, baseline_frames: list) -> dict:
    """Fallback scoring using file hash similarity."""
    screenshot_hash = _file_hash(screenshot_path)

    best_match_idx = 0
    best_distance = float("inf")

    for i, frame_path in enumerate(baseline_frames):
        frame_hash = _file_hash(frame_path)
        # XOR-based hash distance
        distance = sum(a != b for a, b in zip(screenshot_hash, frame_hash))
        if distance < best_distance:
            best_distance = distance
            best_match_idx = i

    num_frames = len(baseline_frames)
    boot_progress = (best_match_idx + 1) / num_frames
    similarity = 1.0 - (best_distance / max(len(screenshot_hash), 1))

    return {
        "boot_progress": round(boot_progress, 4),
        "screenshot_match": round(max(0, similarity), 4),
        "matched_frame": best_match_idx,
    }


def _file_hash(path: Path) -> str:
    """MD5 hash of file contents."""
    return hashlib.md5(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run v86 emulation benchmark")
    parser.add_argument("--duration", type=float, default=EMULATION_DURATION,
                        help=f"Emulation duration in seconds (default: {EMULATION_DURATION})")
    parser.add_argument("--iso", type=str, default=None, help="Path to ISO file")
    parser.add_argument("--skip-build", action="store_true", help="Skip v86 rebuild")
    args = parser.parse_args()

    iso_path = Path(args.iso) if args.iso else DEFAULT_ISO_PATH
    if not iso_path.exists():
        print(f"ERROR: ISO not found at {iso_path}. Run prepare.py first.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Build v86
    build_time = 0.0
    if not args.skip_build:
        build_time = build_v86()

    # Step 2: Run emulation
    print(f"Running emulation benchmark ({args.duration}s)...")
    emu_metrics = run_emulation(iso_path, args.duration)

    # Step 3: Score against baseline
    score = score_boot_progress(SCREENSHOT_PATH)

    # Step 4: Compute derived metrics
    instructions_per_sec = emu_metrics.get("instructions_per_sec", 0)
    jit_cache_hits = emu_metrics.get("jit_cache_hits", 0)
    wasm_compile_time_ms = round(build_time * 1000, 1)  # rough proxy
    memory_usage_mb = emu_metrics.get("memory_usage_mb", 0)

    # Step 5: Print summary
    print("---")
    print(f"boot_progress:        {score['boot_progress']:.4f}")
    print(f"instructions_per_sec: {instructions_per_sec}")
    print(f"jit_cache_hits:       {jit_cache_hits}")
    print(f"wasm_compile_time_ms: {wasm_compile_time_ms}")
    print(f"memory_usage_mb:      {memory_usage_mb}")
    print(f"screenshot_match:     {score['screenshot_match']:.4f}")
    print(f"total_seconds:        {emu_metrics.get('total_seconds', 0)}")


if __name__ == "__main__":
    main()
