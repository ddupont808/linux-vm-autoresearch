"""
Baseline preparation for linux-vm-autoresearch.
Clones v86, builds it, boots the configured ISO, and records a baseline video.

Usage:
    python prepare.py                    # full prep (clone + build + record baseline)
    python prepare.py --clone-v86        # clone v86 only
    python prepare.py --build-only       # build v86 only (skip baseline recording)
    python prepare.py --iso PATH         # use a custom ISO file
    python prepare.py --duration SECS    # baseline recording duration (default: 30)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (fixed, do not modify)
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent.resolve()
V86_DIR = PROJECT_DIR / "v86-workdir"
V86_REPO = "https://github.com/copy/v86.git"
BASELINE_DIR = PROJECT_DIR / "baseline"
BASELINE_VIDEO = BASELINE_DIR / "baseline_video.mp4"
BASELINE_FRAMES_DIR = BASELINE_DIR / "baseline_frames"
BASELINE_META = BASELINE_DIR / "baseline_meta.json"

# Default ISO — Alpine Linux (small, fast boot, good for benchmarking)
DEFAULT_ISO_URL = "https://dl-cdn.alpinelinux.org/alpine/v3.18/releases/x86/alpine-virt-3.18.4-x86.iso"
DEFAULT_ISO_PATH = PROJECT_DIR / "iso" / "alpine-virt-3.18.4-x86.iso"

EMULATION_DURATION = 30  # seconds for baseline recording
SCREENSHOT_INTERVAL = 0.5  # seconds between baseline frames
V86_BUILD_CMD = ["make", "build/v86.wasm"]

# ---------------------------------------------------------------------------
# v86 clone and build
# ---------------------------------------------------------------------------

def clone_v86():
    """Clone the v86 repository."""
    if V86_DIR.exists() and (V86_DIR / ".git").exists():
        print(f"v86 already cloned at {V86_DIR}")
        return

    print(f"Cloning v86 from {V86_REPO}...")
    subprocess.run(
        ["git", "clone", "--depth", "1", V86_REPO, str(V86_DIR)],
        check=True,
    )
    print(f"v86 cloned to {V86_DIR}")


def build_v86():
    """Build v86 from source (requires Rust toolchain + wasm target)."""
    print("Building v86...")

    # Check prerequisites
    for tool in ["rustc", "cargo", "make"]:
        if shutil.which(tool) is None:
            print(f"ERROR: '{tool}' not found. Install Rust toolchain and make.", file=sys.stderr)
            sys.exit(1)

    # Ensure wasm32 target is installed
    subprocess.run(
        ["rustup", "target", "add", "wasm32-unknown-unknown"],
        check=True,
        cwd=str(V86_DIR),
    )

    # Build
    env = os.environ.copy()
    result = subprocess.run(
        V86_BUILD_CMD,
        cwd=str(V86_DIR),
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print("v86 built successfully.")


# ---------------------------------------------------------------------------
# ISO download
# ---------------------------------------------------------------------------

def download_iso(iso_path: Path = DEFAULT_ISO_PATH):
    """Download the default ISO if not present."""
    if iso_path.exists():
        print(f"ISO already present at {iso_path}")
        return iso_path

    iso_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading ISO from {DEFAULT_ISO_URL}...")

    try:
        import requests
        response = requests.get(DEFAULT_ISO_URL, stream=True, timeout=60)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(iso_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = 100 * downloaded / total
                        print(f"\r  {pct:.1f}% ({downloaded // (1024*1024)} MB)", end="", flush=True)
        print()
    except ImportError:
        # Fallback to curl/wget
        if shutil.which("curl"):
            subprocess.run(["curl", "-L", "-o", str(iso_path), DEFAULT_ISO_URL], check=True)
        elif shutil.which("wget"):
            subprocess.run(["wget", "-O", str(iso_path), DEFAULT_ISO_URL], check=True)
        else:
            print("ERROR: Install 'requests' or have curl/wget available.", file=sys.stderr)
            sys.exit(1)

    print(f"ISO downloaded to {iso_path}")
    return iso_path


# ---------------------------------------------------------------------------
# Baseline recording
# ---------------------------------------------------------------------------

def record_baseline(iso_path: Path, duration: float = EMULATION_DURATION):
    """
    Boot the ISO using v86 (Node.js) and record screenshots at regular intervals.
    These frames serve as the baseline "boot video" that emulate.py scores against.
    """
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # Clear old frames
    for f in BASELINE_FRAMES_DIR.glob("*.png"):
        f.unlink()

    print(f"Recording baseline boot sequence ({duration}s)...")
    print(f"  ISO: {iso_path}")
    print(f"  Output: {BASELINE_FRAMES_DIR}")

    # We use a Node.js script to run v86 headlessly and capture screenshots
    recorder_script = PROJECT_DIR / "baseline_recorder.js"
    _write_recorder_script(recorder_script, iso_path, duration)

    result = subprocess.run(
        ["node", str(recorder_script)],
        cwd=str(V86_DIR),
        capture_output=True,
        text=True,
        timeout=int(duration * 3 + 60),  # generous timeout
    )

    if result.returncode != 0:
        print(f"Baseline recording failed:\n{result.stderr}", file=sys.stderr)
        # Don't exit — allow manual frame placement
        print("\nYou can manually place baseline PNG frames in baseline/baseline_frames/")
        print("Name them frame_0000.png, frame_0001.png, etc.")
        return

    # Count captured frames
    frames = sorted(BASELINE_FRAMES_DIR.glob("frame_*.png"))
    print(f"Captured {len(frames)} baseline frames.")

    # Save metadata
    meta = {
        "iso": str(iso_path),
        "duration": duration,
        "interval": SCREENSHOT_INTERVAL,
        "num_frames": len(frames),
        "timestamp": time.time(),
    }
    with open(BASELINE_META, "w") as f:
        json.dump(meta, f, indent=2)

    # Optionally assemble video (if ffmpeg available)
    if shutil.which("ffmpeg") and len(frames) > 0:
        print("Assembling baseline video with ffmpeg...")
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(1.0 / SCREENSHOT_INTERVAL),
            "-i", str(BASELINE_FRAMES_DIR / "frame_%04d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(BASELINE_VIDEO),
        ], capture_output=True)
        if BASELINE_VIDEO.exists():
            print(f"Baseline video saved to {BASELINE_VIDEO}")
    else:
        print("ffmpeg not found — skipping video assembly. Frames are sufficient for scoring.")


def _write_recorder_script(path: Path, iso_path: Path, duration: float):
    """Generate a Node.js script that runs v86 headlessly and captures frames."""
    iso_abs = iso_path.resolve()
    frames_abs = BASELINE_FRAMES_DIR.resolve()
    interval_ms = int(SCREENSHOT_INTERVAL * 1000)
    duration_ms = int(duration * 1000)

    # Use forward slashes for Node.js compatibility
    iso_str = str(iso_abs).replace("\\", "/")
    frames_str = str(frames_abs).replace("\\", "/")
    v86_str = str(V86_DIR.resolve()).replace("\\", "/")

    script = f"""
"use strict";
const fs = require("fs");
const path = require("path");

// v86 can be loaded as a Node.js module
const V86 = require("{v86_str}/build/libv86.js").V86;

const ISO_PATH = "{iso_str}";
const FRAMES_DIR = "{frames_str}";
const INTERVAL_MS = {interval_ms};
const DURATION_MS = {duration_ms};

async function main() {{
    const isoBuffer = fs.readFileSync(ISO_PATH);

    const emulator = new V86({{
        bios: {{ url: path.join("{v86_str}", "bios/seabios.bin") }},
        vga_bios: {{ url: path.join("{v86_str}", "bios/vgabios.bin") }},
        cdrom: {{ buffer: isoBuffer }},
        memory_size: 256 * 1024 * 1024,
        vga_memory_size: 8 * 1024 * 1024,
        autostart: true,
        screen_dummy: true,
    }});

    let frameIndex = 0;
    const startTime = Date.now();

    const captureInterval = setInterval(() => {{
        const elapsed = Date.now() - startTime;
        if (elapsed >= DURATION_MS) {{
            clearInterval(captureInterval);
            console.log("Baseline recording complete.");
            emulator.stop();
            process.exit(0);
        }}

        try {{
            const screenData = emulator.screen_make_screenshot();
            if (screenData) {{
                const framePath = path.join(FRAMES_DIR,
                    "frame_" + String(frameIndex).padStart(4, "0") + ".png");
                fs.writeFileSync(framePath, screenData);
                frameIndex++;
                const pct = ((elapsed / DURATION_MS) * 100).toFixed(1);
                process.stdout.write("\\rFrame " + frameIndex + " (" + pct + "%)");
            }}
        }} catch (e) {{
            // Screenshot might not be available yet during early boot
        }}
    }}, INTERVAL_MS);

    // Safety timeout
    setTimeout(() => {{
        clearInterval(captureInterval);
        console.log("\\nTimeout reached. Stopping.");
        try {{ emulator.stop(); }} catch(e) {{}}
        process.exit(0);
    }}, DURATION_MS + 10000);
}}

main().catch(err => {{
    console.error(err);
    process.exit(1);
}});
"""
    path.write_text(script)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare baseline for linux-vm-autoresearch")
    parser.add_argument("--clone-v86", action="store_true", help="Clone v86 only")
    parser.add_argument("--build-only", action="store_true", help="Build v86 only")
    parser.add_argument("--iso", type=str, default=None, help="Path to custom ISO file")
    parser.add_argument("--duration", type=float, default=EMULATION_DURATION,
                        help=f"Baseline recording duration in seconds (default: {EMULATION_DURATION})")
    args = parser.parse_args()

    iso_path = Path(args.iso) if args.iso else DEFAULT_ISO_PATH

    if args.clone_v86:
        clone_v86()
        sys.exit(0)

    # Step 1: Clone v86
    clone_v86()

    # Step 2: Build v86
    build_v86()

    if args.build_only:
        print("\nBuild complete. Skipping baseline recording.")
        sys.exit(0)

    # Step 3: Download ISO if needed
    if not iso_path.exists():
        iso_path = download_iso(iso_path)

    # Step 4: Record baseline
    record_baseline(iso_path, duration=args.duration)

    print("\nDone! Ready to experiment.")
    print(f"  v86 source:  {V86_DIR}")
    print(f"  Baseline:    {BASELINE_DIR}")
    print(f"  Target file: {V86_DIR / 'src' / 'rust' / 'jit.rs'}")
