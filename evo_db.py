"""
Evolutionary database for linux-vm-autoresearch experiments.
Population-based search using MAP-Elites + island model.
Adapted from hgarud/autoresearch's evo_db.py for JIT optimization.

Usage:
    python evo_db.py add --commit abc1234 --parent <id> --description "..." --log run.log
    python evo_db.py add-crash --commit abc1234 --parent <id> --description "..."
    python evo_db.py sample
    python evo_db.py status
    python evo_db.py best
    python evo_db.py history [--limit N]
    python evo_db.py config
    python evo_db.py config set <key> <value>
    python evo_db.py config reset
"""

import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULTS = {
    "num_islands": 2,
    "grid_size": 5,
    "archive_size": 20,
    "population_limit": 200,
    "migration_interval": 15,
    "p_exploit": 0.70,
    "p_explore": 0.20,
    "p_random": 0.10,
    # Feature dimensions for MAP-Elites grid:
    # boot_progress (primary metric) vs memory_usage_mb
    "feature_dims": ["boot_progress", "memory_usage_mb"],
    # All metrics to extract from run logs
    "metric_keys": [
        "boot_progress", "instructions_per_sec", "jit_cache_hits",
        "wasm_compile_time_ms", "memory_usage_mb", "screenshot_match",
    ],
}

DB_PATH = os.path.join(os.getcwd(), "evo_db.json")
CONFIG_PATH = os.path.join(os.getcwd(), "evo_db_config.json")


def _load_config() -> dict:
    config = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        config.update(user)
    config["feature_dims"] = tuple(config["feature_dims"])
    return config


def _save_config(config: dict):
    out = {}
    for k, v in config.items():
        out[k] = list(v) if isinstance(v, tuple) else v
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


_cfg = _load_config()

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_STRUCTURAL_KEYS = frozenset({
    "id", "commit", "parent_id", "generation", "island",
    "timestamp", "description", "status",
})


@dataclass
class Experiment:
    id: str
    commit: str
    parent_id: Optional[str]
    generation: int
    island: int
    timestamp: float
    description: str
    status: str  # "success" | "crash"
    metrics: dict = field(default_factory=dict)

    @property
    def boot_progress(self) -> float:
        return self.metrics.get("boot_progress", 0.0)

    def to_dict(self):
        d = {
            "id": self.id,
            "commit": self.commit,
            "parent_id": self.parent_id,
            "generation": self.generation,
            "island": self.island,
            "timestamp": self.timestamp,
            "description": self.description,
            "status": self.status,
        }
        d.update(self.metrics)
        return d

    @classmethod
    def from_dict(cls, d):
        metrics = {k: v for k, v in d.items() if k not in _STRUCTURAL_KEYS}
        return cls(
            id=d["id"],
            commit=d["commit"],
            parent_id=d["parent_id"],
            generation=d["generation"],
            island=d["island"],
            timestamp=d["timestamp"],
            description=d["description"],
            status=d["status"],
            metrics=metrics,
        )


class EvoDB:
    def __init__(self):
        self.experiments: Dict[str, Experiment] = {}
        self.island_grids: List[Dict[str, str]] = [{} for _ in range(_cfg["num_islands"])]
        self.islands: List[Set[str]] = [set() for _ in range(_cfg["num_islands"])]
        self.archive: List[str] = []
        self.best_id: Optional[str] = None
        self.feature_stats: Dict[str, Dict] = {
            dim: {"min": float("inf"), "max": float("-inf")}
            for dim in _cfg["feature_dims"]
        }
        self._next_island: int = 0
        self._add_count: int = 0
        self._migrated: Set[str] = set()

    # -------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------

    def save(self):
        data = {
            "experiments": {k: v.to_dict() for k, v in self.experiments.items()},
            "island_grids": self.island_grids,
            "islands": [list(s) for s in self.islands],
            "archive": self.archive,
            "best_id": self.best_id,
            "feature_stats": self.feature_stats,
            "_next_island": self._next_island,
            "_add_count": self._add_count,
            "_migrated": list(self._migrated),
        }
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DB_PATH)

    @classmethod
    def load(cls) -> "EvoDB":
        db = cls()
        if not os.path.exists(DB_PATH):
            return db
        with open(DB_PATH) as f:
            data = json.load(f)
        for k, v in data["experiments"].items():
            db.experiments[k] = Experiment.from_dict(v)
        db.island_grids = data["island_grids"]
        db.islands = [set(s) for s in data["islands"]]
        db.archive = data["archive"]
        db.best_id = data["best_id"]
        db.feature_stats = data["feature_stats"]
        db._next_island = data.get("_next_island", 0)
        db._add_count = data.get("_add_count", 0)
        db._migrated = set(data.get("_migrated", []))
        return db

    # -------------------------------------------------------------------
    # Feature binning
    # -------------------------------------------------------------------

    def _update_feature_stats(self, exp: Experiment):
        for dim in _cfg["feature_dims"]:
            val = exp.metrics.get(dim, 0.0)
            stats = self.feature_stats[dim]
            if val < stats["min"]:
                stats["min"] = val
            if val > stats["max"]:
                stats["max"] = val

    def _get_bin(self, exp: Experiment) -> Tuple[int, int]:
        grid_size = _cfg["grid_size"]
        bins = []
        for dim in _cfg["feature_dims"]:
            val = exp.metrics.get(dim, 0.0)
            stats = self.feature_stats[dim]
            lo, hi = stats["min"], stats["max"]
            if hi <= lo:
                b = grid_size // 2
            else:
                frac = (val - lo) / (hi - lo)
                b = min(int(frac * grid_size), grid_size - 1)
            bins.append(b)
        return (bins[0], bins[1])

    def _bin_key(self, row: int, col: int) -> str:
        return f"{row}-{col}"

    # -------------------------------------------------------------------
    # Core: add experiment
    # -------------------------------------------------------------------

    def add(self, exp: Experiment) -> str:
        self.experiments[exp.id] = exp
        island = exp.island

        while island >= len(self.islands):
            self.islands.append(set())
            self.island_grids.append({})

        self.islands[island].add(exp.id)

        if exp.status == "success":
            self._update_feature_stats(exp)
            self._rebuild_grids()
            self._update_archive(exp)
            self._update_best(exp)

        self._add_count += 1

        if self._add_count % _cfg["migration_interval"] == 0:
            self._migrate()

        self._enforce_population_limit()
        self.save()
        return exp.id

    def _rebuild_grids(self):
        self.island_grids = [{} for _ in range(_cfg["num_islands"])]
        for exp in self.experiments.values():
            if exp.status != "success":
                continue
            self._place_in_grid(exp)

    def _place_in_grid(self, exp: Experiment):
        row, col = self._get_bin(exp)
        key = self._bin_key(row, col)
        grid = self.island_grids[exp.island]
        existing_id = grid.get(key)
        if existing_id is None:
            grid[key] = exp.id
        else:
            existing = self.experiments.get(existing_id)
            # Higher boot_progress is better (unlike val_bpb which is lower-is-better)
            if existing is None or exp.boot_progress > existing.boot_progress:
                grid[key] = exp.id

    def _update_archive(self, exp: Experiment):
        if exp.id not in self.archive:
            self.archive.append(exp.id)
        # Sort by boot_progress descending (higher is better)
        self.archive.sort(key=lambda eid: self.experiments[eid].boot_progress, reverse=True)
        self.archive = self.archive[:_cfg["archive_size"]]

    def _update_best(self, exp: Experiment):
        if self.best_id is None:
            self.best_id = exp.id
        else:
            best = self.experiments[self.best_id]
            if exp.boot_progress > best.boot_progress:
                self.best_id = exp.id

    def _enforce_population_limit(self):
        total = len(self.experiments)
        if total <= _cfg["population_limit"]:
            return

        protected = set()
        if self.best_id:
            protected.add(self.best_id)
        protected.update(self.archive)
        for grid in self.island_grids:
            protected.update(grid.values())

        # Sort by boot_progress ascending (worst first for removal)
        candidates = [eid for eid in self.experiments if eid not in protected]
        candidates.sort(key=lambda eid: self.experiments[eid].boot_progress)

        to_remove = total - _cfg["population_limit"]
        for eid in candidates[:to_remove]:
            exp = self.experiments[eid]
            self.islands[exp.island].discard(eid)
            del self.experiments[eid]

    # -------------------------------------------------------------------
    # Migration
    # -------------------------------------------------------------------

    def _migrate(self):
        num_islands = _cfg["num_islands"]
        if num_islands < 2:
            return

        for src in range(num_islands):
            dst = (src + 1) % num_islands
            best_id = None
            best_progress = -1.0
            for eid in self.islands[src]:
                exp = self.experiments.get(eid)
                if exp is None or exp.status != "success":
                    continue
                if eid in self._migrated:
                    continue
                if exp.boot_progress > best_progress:
                    best_progress = exp.boot_progress
                    best_id = eid
            if best_id is None:
                continue

            src_exp = self.experiments[best_id]
            self._migrated.add(best_id)

            migrant = Experiment(
                id=str(uuid.uuid4())[:8],
                commit=src_exp.commit,
                parent_id=src_exp.id,
                generation=src_exp.generation,
                island=dst,
                timestamp=time.time(),
                description=f"[migrated from island {src}] {src_exp.description}",
                status=src_exp.status,
                metrics=dict(src_exp.metrics),
            )
            self.experiments[migrant.id] = migrant
            self.islands[dst].add(migrant.id)
            self._migrated.add(migrant.id)
            self._update_feature_stats(migrant)
            self._place_in_grid(migrant)
            self._update_archive(migrant)

    # -------------------------------------------------------------------
    # Sampling
    # -------------------------------------------------------------------

    def sample(self) -> dict:
        success_exps = [e for e in self.experiments.values() if e.status == "success"]

        if not success_exps:
            return {
                "parent": None,
                "inspirations": [],
                "strategy": "initial",
                "island": self._next_island,
                "suggestion": "No experiments yet. Run the baseline first.",
            }

        island = self._next_island
        self._next_island = (self._next_island + 1) % _cfg["num_islands"]

        roll = random.random()
        if roll < _cfg["p_exploit"]:
            strategy = "exploit"
        elif roll < _cfg["p_exploit"] + _cfg["p_explore"]:
            strategy = "explore"
        else:
            strategy = "random"

        parent = self._select_parent(strategy, island, success_exps)
        inspirations = self._select_inspirations(parent, island, success_exps)
        suggestion = self._make_suggestion(parent, inspirations, strategy)

        result = {
            "parent": parent.to_dict(),
            "inspirations": [e.to_dict() for e in inspirations],
            "strategy": strategy,
            "island": island,
            "suggestion": suggestion,
        }

        self.save()
        return result

    def _select_parent(self, strategy: str, island: int,
                       success_exps: List[Experiment]) -> Experiment:
        island_exps = [e for e in success_exps if e.island == island]

        if strategy == "exploit":
            if self.archive:
                eid = random.choice(self.archive[:5])
                return self.experiments[eid]
            return max(success_exps, key=lambda e: e.boot_progress)

        elif strategy == "explore":
            grid = self.island_grids[island]
            if grid:
                cell_exps = [
                    self.experiments[eid] for eid in grid.values()
                    if eid in self.experiments
                ]
                if cell_exps:
                    # Weight toward lower (worse) boot_progress — explore weaker regions
                    cell_exps.sort(key=lambda e: e.boot_progress)
                    pool = cell_exps[:max(1, len(cell_exps) // 2)]
                    return random.choice(pool)
            pool = island_exps if island_exps else success_exps
            return random.choice(pool)

        else:
            return random.choice(success_exps)

    def _select_inspirations(self, parent: Experiment, island: int,
                             success_exps: List[Experiment]) -> List[Experiment]:
        num_islands = _cfg["num_islands"]
        inspirations = []
        used_ids = {parent.id}

        grid = self.island_grids[island]
        parent_bin = self._get_bin(parent)
        parent_key = self._bin_key(*parent_bin)
        diff_region = [
            self.experiments[eid] for key, eid in grid.items()
            if key != parent_key and eid in self.experiments
        ]
        if diff_region:
            pick = random.choice(diff_region)
            if pick.id not in used_ids:
                inspirations.append(pick)
                used_ids.add(pick.id)

        other_island = (island + 1) % num_islands
        other_exps = [
            e for e in success_exps
            if e.island == other_island and e.id not in used_ids
        ]
        if other_exps:
            pick = random.choice(other_exps)
            inspirations.append(pick)
            used_ids.add(pick.id)

        archive_picks = [
            self.experiments[eid] for eid in self.archive
            if eid not in used_ids and eid in self.experiments
        ]
        if archive_picks:
            pick = random.choice(archive_picks[:10])
            inspirations.append(pick)
            used_ids.add(pick.id)

        return inspirations[:3]

    def _make_suggestion(self, parent: Experiment,
                         inspirations: List[Experiment],
                         strategy: str) -> str:
        lines = []

        if strategy == "exploit":
            lines.append(
                f"EXPLOIT: Build on a top performer (boot_progress={parent.boot_progress:.4f}). "
                f"Try incremental JIT improvements — tweak code generation, improve caching, optimize hot paths."
            )
        elif strategy == "explore":
            lines.append(
                f"EXPLORE: Try something different from parent (boot_progress={parent.boot_progress:.4f}). "
                f"Consider a different compilation strategy, instruction fusion approach, or register allocation scheme."
            )
        else:
            lines.append(
                f"RANDOM: Wild card — start from parent (boot_progress={parent.boot_progress:.4f}) "
                f"but try something bold or unconventional in jit.rs."
            )

        metric_strs = _format_metric_summary(parent.metrics)
        lines.append(f"Parent: commit={parent.commit}, \"{parent.description}\""
                      + (f" ({metric_strs})" if metric_strs else ""))

        if inspirations:
            lines.append("Inspirations:")
            for i, insp in enumerate(inspirations, 1):
                metric_strs = _format_metric_summary(insp.metrics)
                lines.append(
                    f"  {i}. commit={insp.commit}, \"{insp.description}\""
                    + (f" ({metric_strs})" if metric_strs else "")
                )

        return "\n".join(lines)

    # -------------------------------------------------------------------
    # Query methods
    # -------------------------------------------------------------------

    def get_best(self) -> Optional[Experiment]:
        if self.best_id and self.best_id in self.experiments:
            return self.experiments[self.best_id]
        return None

    def get_history(self, limit: int = 10) -> List[Experiment]:
        exps = sorted(self.experiments.values(),
                       key=lambda e: e.timestamp, reverse=True)
        return exps[:limit]

    def get_status(self) -> dict:
        total = len(self.experiments)
        successes = sum(1 for e in self.experiments.values() if e.status == "success")
        crashes = total - successes
        best = self.get_best()

        island_counts = [len(s) for s in self.islands]
        grid_fill = [len(g) for g in self.island_grids]

        return {
            "total_experiments": total,
            "successes": successes,
            "crashes": crashes,
            "best": best.to_dict() if best else None,
            "island_populations": island_counts,
            "grid_fill": grid_fill,
            "grid_capacity": _cfg["grid_size"] * _cfg["grid_size"],
            "archive_size": len(self.archive),
        }

    def _format_grid(self, island: int) -> str:
        grid_size = _cfg["grid_size"]
        feature_dims = _cfg["feature_dims"]
        grid = self.island_grids[island]
        lines = []
        lines.append(f"  Island {island} MAP-Elites ({feature_dims[0]} x {feature_dims[1]}):")

        header = "     " + "".join(f"  {c:>2}  " for c in range(grid_size))
        lines.append(header)

        for r in range(grid_size):
            row_str = f"  {r:>2} "
            for c in range(grid_size):
                key = self._bin_key(r, c)
                eid = grid.get(key)
                if eid and eid in self.experiments:
                    exp = self.experiments[eid]
                    row_str += f" {exp.boot_progress:.3f}"
                else:
                    row_str += "   -  "
            lines.append(row_str)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_metric_val(val) -> str:
    if isinstance(val, int):
        return str(val)
    return f"{val:.6g}"


def _format_metric_summary(metrics: dict) -> str:
    parts = []
    for key in _cfg["metric_keys"]:
        val = metrics.get(key)
        if val is not None:
            parts.append(f"{key}={_format_metric_val(val)}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def parse_run_log(log_path: str) -> dict:
    metric_keys = _cfg["metric_keys"]
    metrics = {}
    in_summary = False
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line == "---":
                in_summary = True
                continue
            if in_summary:
                match = re.match(r"^(\w+):\s+([\d.]+)$", line)
                if match:
                    key, val = match.group(1), match.group(2)
                    if key in metric_keys:
                        if "." in val:
                            metrics[key] = float(val)
                        else:
                            metrics[key] = int(val)
    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_add(args):
    db = EvoDB.load()
    metrics = parse_run_log(args.log)
    missing = [k for k in ["boot_progress"] if k not in metrics]
    if missing:
        print(f"ERROR: Missing metrics in {args.log}: {missing}", file=sys.stderr)
        sys.exit(1)

    parent_id = args.parent
    generation = 0
    if parent_id and parent_id in db.experiments:
        generation = db.experiments[parent_id].generation + 1

    island = db._next_island

    exp = Experiment(
        id=str(uuid.uuid4())[:8],
        commit=args.commit[:7],
        parent_id=parent_id,
        generation=generation,
        island=island,
        timestamp=time.time(),
        description=args.description,
        status="success",
        metrics=metrics,
    )

    db.add(exp)
    print(json.dumps({"id": exp.id, "boot_progress": exp.boot_progress, "island": exp.island}))


def cmd_add_crash(args):
    db = EvoDB.load()

    parent_id = args.parent
    generation = 0
    if parent_id and parent_id in db.experiments:
        generation = db.experiments[parent_id].generation + 1

    island = db._next_island

    exp = Experiment(
        id=str(uuid.uuid4())[:8],
        commit=args.commit[:7],
        parent_id=parent_id,
        generation=generation,
        island=island,
        timestamp=time.time(),
        description=args.description,
        status="crash",
    )

    db.add(exp)
    print(json.dumps({"id": exp.id, "status": "crash", "island": exp.island}))


def cmd_sample(args):
    db = EvoDB.load()
    result = db.sample()
    print(json.dumps(result, indent=2))


def cmd_status(args):
    db = EvoDB.load()
    status = db.get_status()

    print(f"Population: {status['total_experiments']} experiments "
          f"({status['successes']} success, {status['crashes']} crash)")
    print(f"Archive: {status['archive_size']}/{_cfg['archive_size']}")
    for i in range(_cfg["num_islands"]):
        print(f"Island {i}: {status['island_populations'][i]} experiments, "
              f"{status['grid_fill'][i]}/{status['grid_capacity']} grid cells filled")

    if status["best"]:
        b = status["best"]
        metric_parts = []
        for key in _cfg["metric_keys"]:
            if key in b:
                metric_parts.append(f"{key}={_format_metric_val(b[key])}")
        print(f"\nBest: id={b['id']} commit={b['commit']} {' '.join(metric_parts)}")
        print(f"  \"{b['description']}\"")

    for i in range(_cfg["num_islands"]):
        print()
        print(db._format_grid(i))

    if db.archive:
        print(f"\nArchive (top {len(db.archive)}):")
        for rank, eid in enumerate(db.archive, 1):
            exp = db.experiments[eid]
            metric_strs = _format_metric_summary(exp.metrics)
            print(f"  {rank:>2}. id={exp.id} commit={exp.commit} "
                  f"{metric_strs} island={exp.island}")


def cmd_best(args):
    db = EvoDB.load()
    best = db.get_best()
    if best is None:
        print("No experiments yet.")
        return
    print(json.dumps(best.to_dict(), indent=2))


def cmd_history(args):
    db = EvoDB.load()
    limit = args.limit
    history = db.get_history(limit)
    if not history:
        print("No experiments yet.")
        return

    metric_keys = _cfg["metric_keys"]

    header = f"{'id':>8}  {'commit':>7}  {'status':>7}"
    for key in metric_keys:
        header += f"  {key:>18}"
    header += f"  {'isl':>3}  {'gen':>3}  description"
    print(header)
    print("-" * len(header))
    for exp in history:
        line = f"{exp.id:>8}  {exp.commit:>7}  {exp.status:>7}"
        for key in metric_keys:
            val = exp.metrics.get(key)
            if val is not None and exp.status == "success":
                if isinstance(val, int):
                    line += f"  {val:>18}"
                else:
                    line += f"  {val:>18.6f}"
            else:
                line += f"  {'-':>18}"
        line += f"  {exp.island:>3}  {exp.generation:>3}  {exp.description}"
        print(line)


def cmd_config(args):
    if args.config_action == "set":
        config = _load_config()
        key = args.key
        value = args.value
        if key not in DEFAULTS:
            print(f"ERROR: Unknown config key '{key}'", file=sys.stderr)
            sys.exit(1)
        default = DEFAULTS[key]
        if isinstance(default, int):
            config[key] = int(value)
        elif isinstance(default, float):
            config[key] = float(value)
        elif isinstance(default, list):
            config[key] = [v.strip() for v in value.split(",")]
        else:
            config[key] = value
        _save_config(config)
        print(f"{key} = {config[key]}")
    elif args.config_action == "reset":
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        print("Config reset to defaults.")
    else:
        config = _load_config()
        print(f"Config: {CONFIG_PATH}")
        has_file = os.path.exists(CONFIG_PATH)
        print(f"Custom config file: {'yes' if has_file else 'no (using defaults)'}")
        print()
        for key in sorted(DEFAULTS.keys()):
            current = config[key]
            default = DEFAULTS[key]
            default_cmp = tuple(default) if isinstance(default, list) else default
            current_cmp = tuple(current) if isinstance(current, (list, tuple)) else current
            marker = " *" if current_cmp != default_cmp else ""
            print(f"  {key}: {current}{marker}")
        if has_file:
            print()
            print("  (* = non-default custom value)")


def main():
    parser = argparse.ArgumentParser(
        description="Evolutionary experiment database for JIT optimization",
        usage="python evo_db.py <command> [options]",
    )
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="Record a successful experiment")
    p_add.add_argument("--commit", required=True, help="Git commit hash")
    p_add.add_argument("--parent", default=None, help="Parent experiment id")
    p_add.add_argument("--description", required=True, help="What this experiment tried")
    p_add.add_argument("--log", required=True, help="Path to run.log")

    p_crash = sub.add_parser("add-crash", help="Record a crashed experiment")
    p_crash.add_argument("--commit", required=True, help="Git commit hash")
    p_crash.add_argument("--parent", default=None, help="Parent experiment id")
    p_crash.add_argument("--description", required=True, help="What this experiment tried")

    sub.add_parser("sample", help="Sample next parent + inspirations")
    sub.add_parser("status", help="Show population status")
    sub.add_parser("best", help="Show best experiment")

    p_hist = sub.add_parser("history", help="Show recent experiments")
    p_hist.add_argument("--limit", type=int, default=10)

    p_cfg = sub.add_parser("config", help="View or modify configuration")
    cfg_sub = p_cfg.add_subparsers(dest="config_action")
    p_cfg_set = cfg_sub.add_parser("set", help="Set a config value")
    p_cfg_set.add_argument("key")
    p_cfg_set.add_argument("value")
    cfg_sub.add_parser("reset", help="Reset config to defaults")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args)
    elif args.command == "add-crash":
        cmd_add_crash(args)
    elif args.command == "sample":
        cmd_sample(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "best":
        cmd_best(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "config":
        cmd_config(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
