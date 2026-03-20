# linux-vm-autoresearch

This is an experiment to have an LLM autonomously optimize x86 JIT compilation in v86 for faster Linux boot emulation, using an evolutionary database to track and guide experiments.

## Setup

To set up a new experiment, work with the user to:

1. **Agree on a run tag**: propose a tag based on today's date (e.g. `mar19`). The branch `autoresearch/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b autoresearch/<tag>` from current master.
3. **Read the in-scope files**: Read these files for full context:
   - `README.md` — repository context.
   - `prepare.py` — baseline recording. Boots the configured ISO and records a reference video. Do not modify.
   - `emulate.py` — builds v86, runs the ISO for a fixed duration, takes a screenshot, and scores how far into the baseline boot the modified v86 reached. Do not modify.
   - `evo_db.py` — evolutionary database (MAP-Elites + island model). Do not modify.
   - `train.py` — the experiment loop driver. Do not modify.
   - `v86-workdir/` — the v86 source checkout. Only the JIT pipeline files are editable (see below).
4. **Verify v86 is cloned**: Check that `v86-workdir/` contains the v86 source. If not, tell the human to run `python prepare.py --clone-v86`.
5. **Verify baseline exists**: Check that `baseline/` contains `baseline_video.mp4` and `baseline_frames/`. If not, tell the human to run `python prepare.py`.
6. **Confirm and go**: Confirm setup looks good.

Once you get confirmation, kick off the experimentation.

## The Task

v86 is a JavaScript/WebAssembly x86 emulator that can boot full Linux ISOs in the browser. The core JIT (Just-In-Time) compiler lives in `src/rust/jit.rs` and translates x86 instructions into WebAssembly on the fly.

**The goal**: Optimize `src/rust/jit.rs` to make the emulator boot the configured Linux ISO faster. The metric is "boot progress" — how far into the baseline boot sequence the emulator gets within a fixed time window (default: 5 seconds of emulated time).

## Experimentation

Each experiment runs the emulation benchmark for a **fixed time budget of 5 seconds** (wall clock). You launch it via: `python emulate.py`.

**What you CAN do:**
- Modify the JIT compilation pipeline files — these are the ONLY files you edit:
  - `v86-workdir/src/rust/jit.rs` (2,443 lines) — JIT entry point, cache management, compilation triggers, hot path detection
  - `v86-workdir/src/rust/jit_instructions.rs` (7,881 lines) — per-instruction x86→wasm code generation (the largest optimization surface)
  - `v86-workdir/src/rust/codegen.rs` (2,660 lines) — code generation utilities, basic block compilation
  - `v86-workdir/src/rust/control_flow.rs` (425 lines) — control flow analysis for JIT compilation
  - `v86-workdir/src/rust/wasmgen/wasm_builder.rs` (1,047 lines) — WebAssembly bytecode emission
  - `v86-workdir/src/rust/wasmgen/wasm_opcodes.rs` (221 lines) — wasm opcode definitions
  - `v86-workdir/src/rust/profiler.rs` (155 lines) — profiling hooks
- Everything is fair game: instruction dispatch, code generation, caching strategies, optimization passes, register allocation, hot path optimization, branch prediction hints, instruction fusion, wasm emission quality, etc.

**What you CANNOT do:**
- Modify any file outside the JIT pipeline listed above.
- Modify `prepare.py`, `emulate.py`, `evo_db.py`, or `train.py`.
- Install new system packages or add dependencies beyond what v86 already uses.
- Modify the evaluation harness in `emulate.py`.

**The goal is simple: maximize boot_progress (0.0 to 1.0).** Since the time budget is fixed at 5 seconds, you want the emulator to get as far into the boot sequence as possible within that window.

**Secondary metrics** (tracked but not the primary optimization target):
- `instructions_per_sec` — higher is better, indicates JIT throughput
- `jit_cache_hits` — higher is better, indicates effective caching
- `wasm_compile_time_ms` — lower is better, indicates JIT compilation overhead
- `memory_usage_mb` — lower is better, soft constraint

**Simplicity criterion**: All else being equal, simpler is better. A small improvement that adds ugly complexity is not worth it. Removing unnecessary code and getting equal or better results is a great outcome.

**The first run**: Your very first run should always establish the baseline by running `emulate.py` with the unmodified `jit.rs`.

## Output format

Once the emulation finishes, `emulate.py` prints a summary like:

```
---
boot_progress:        0.4500
instructions_per_sec: 125000000
jit_cache_hits:       8923
wasm_compile_time_ms: 234.5
memory_usage_mb:      312.4
screenshot_match:     0.4500
total_seconds:        5.0
```

## Logging results

When an experiment is done, log it to the evolutionary database (`evo_db.py`):

**Recording a successful experiment:**
```bash
python evo_db.py add --commit <hash, short, 7 chars> --parent <id> --description "..." --log run.log
```

**Recording a crashed experiment (build failure, runtime crash, etc.):**
```bash
python evo_db.py add-crash --commit <hash, short, 7 chars> --parent <id> --description "..."
```

**Other useful commands:**
```bash
python evo_db.py sample    # Get next parent + inspirations (JSON)
python evo_db.py status    # Population overview with MAP-Elites grids
python evo_db.py best      # Show best experiment
python evo_db.py history   # Recent experiments
```

## The experiment loop

The experiment runs on a dedicated branch (e.g. `autoresearch/mar19`).

LOOP FOREVER:

1. **SAMPLE**: Run `python evo_db.py sample` to get a parent experiment, inspirations, and a strategy hint (exploit/explore/random). Read the JSON output carefully.
2. **RESTORE parent's code**: For each JIT pipeline file you plan to modify, restore the parent's version:
   ```
   git show <parent_commit>:v86-workdir/src/rust/jit.rs > v86-workdir/src/rust/jit.rs
   git show <parent_commit>:v86-workdir/src/rust/jit_instructions.rs > v86-workdir/src/rust/jit_instructions.rs
   # ... etc for any file you changed
   ```
3. **DESIGN** your change based on the parent code, the inspirations, and the strategy hint. For "exploit", make incremental JIT improvements. For "explore", try something structurally different. For "random", go bold.
4. **EDIT** the relevant JIT pipeline files with your experimental change.
5. **GIT COMMIT** the change.
6. **RUN**: `python emulate.py > run.log 2>&1`
7. **RECORD**:
   - Check results: `grep "^boot_progress:\|^instructions_per_sec:" run.log`
   - If the grep output is empty, the run crashed. Run `tail -n 50 run.log` to read the error and attempt a fix.
   - If success: `python evo_db.py add --commit <hash> --parent <parent_id> --description "..." --log run.log`
   - If crash: `python evo_db.py add-crash --commit <hash> --parent <parent_id> --description "..."`
8. Optionally: `python evo_db.py status` to review population state.
9. **GOTO 1**

## JIT Optimization Ideas

Here are some directions to explore in `jit.rs`:

- **Instruction fusion**: Combine common instruction sequences (e.g. cmp+jcc, push+push, mov+mov) into single wasm operations
- **Hot path detection**: Track frequently executed basic blocks and apply more aggressive optimization
- **Register allocation**: Improve mapping of x86 registers to wasm locals
- **Branch prediction**: Add static branch prediction hints for common patterns (loop back-edges, etc.)
- **Constant folding**: Propagate known constants through JIT'd code
- **Dead code elimination**: Skip generating wasm for unreachable paths
- **Memory access optimization**: Batch or coalesce memory operations
- **JIT cache improvements**: Better eviction policies, larger cache, or tiered compilation
- **Lazy flag computation**: Defer x86 flag calculations until they're actually needed
- **Superblock formation**: Extend basic blocks across taken branches for longer optimization windows

**Timeout**: Each experiment should take ~30 seconds total (5s emulation + build time). If a run exceeds 2 minutes, kill it and record as crash.

**Crashes**: If a build fails (Rust compilation error), fix the obvious issue and re-run. If the idea itself is fundamentally broken, record as crash and move on.

**NEVER STOP**: Once the experiment loop has begun, do NOT pause to ask the human if you should continue. The human might be asleep. You are autonomous. If you run out of ideas, re-read jit.rs for new angles, try combining previous near-misses, or try more radical optimization strategies. The loop runs until the human interrupts you.
