# linux-vm-autoresearch

Autonomous LLM-driven optimization of v86's JIT compiler for faster Linux boot emulation, using an evolutionary database (MAP-Elites + island model) to guide the search.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch), [hgarud's evolutionary DB extension](https://github.com/hgarud/autoresearch), and [Google DeepMind's AlphaEvolve](https://deepmind.google/discover/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/).

## How It Works

1. **prepare.py** — Clones [v86](https://github.com/copy/v86), builds it, boots a Linux ISO, and records a baseline boot video (sequence of screenshots)
2. **emulate.py** — Rebuilds v86 with the current `jit.rs`, runs the ISO for 5 seconds, takes a screenshot, and scores how far into the baseline boot it reached
3. **evo_db.py** — Evolutionary database tracking all experiments with MAP-Elites grid + island model for diverse exploration
4. **train.py** — Experiment loop driver (the LLM agent follows `program.md` to run the loop autonomously)
5. **program.md** — Instructions for the LLM agent: what to modify, how to score, how to log results

## The Target

The LLM agent can **only modify** the JIT compilation pipeline in `v86-workdir/src/rust/` — the files that translate x86 instructions into WebAssembly at runtime:

- `jit.rs` — JIT entry point, cache management, compilation triggers
- `jit_instructions.rs` — per-instruction x86→wasm code generation (largest optimization surface)
- `codegen.rs` — code generation utilities, basic block compilation
- `control_flow.rs` — control flow analysis
- `wasmgen/wasm_builder.rs` — WebAssembly bytecode emission
- `wasmgen/wasm_opcodes.rs` — wasm opcode definitions
- `profiler.rs` — profiling hooks

The goal is to maximize `boot_progress` (0.0 to 1.0): how far the emulator gets through the Linux boot sequence in a fixed 5-second window.

## Quick Start

```bash
# 1. Setup
uv run prepare.py

# 2. Run baseline
uv run train.py --baseline

# 3. Start autonomous experimentation (with an LLM agent)
# Point your LLM agent at program.md and let it run
```

## Project Structure

```
linux-vm-autoresearch/
  program.md          # LLM agent instructions
  prepare.py          # Setup: clone v86, build, record baseline
  emulate.py          # Benchmark: build, run, score
  evo_db.py           # Evolutionary database (MAP-Elites)
  train.py            # Experiment loop driver
  v86-workdir/        # v86 source (only JIT pipeline files are editable)
  baseline/           # Baseline boot frames and video
  iso/                # Linux ISO for benchmarking
  references/         # Reference projects
```

## References

- [hgarud/autoresearch](https://github.com/hgarud/autoresearch) — Evolutionary DB autoresearch (primary inspiration)
- [jsegov/autoresearch-win-rtx](https://github.com/jsegov/autoresearch-win-rtx) — Windows port
- [JackSuuu/Linux-In-Web](https://github.com/JackSuuu/Linux-In-Web) — Browser-based Linux VM using v86
- [copy/v86](https://github.com/copy/v86) — x86 emulator in JavaScript/WebAssembly
- [OpenEvolve](https://github.com/codelion/openevolve) — Open-source evolutionary algorithm framework
