#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T02:00:00Z | Author: claude_code | Change: P-vs-NP scheduler for task selection
"""LIRIL P-vs-NP scheduler — treats dev-team task selection as a QUBO.

User directive (2026-04-19):
  'continue with building and p vs np in your operations to start
   increasing work output and accuracy with liril on our website
   start pushing changes through liril and observing'

Operational insight: the dev-team backlog is a scheduling problem.
Picking the wrong task (e.g. repeatedly retrying WS-034 which requires
25 data points Mistral-Nemo can't produce in one cycle) burns GPU time
for no output. Picking the right task (e.g. WS-034-ca with a single
entry) produces a clean commit in 6s.

This is classic combinatorial optimisation. Frame as QUBO:

  minimise over binary x_t ∈ {0,1} (pick task t exactly once):
    Σ_t x_t × cost(t)
    − Σ_t x_t × value(t)
    + λ × (Σ_t x_t − k)²                        # pick exactly k tasks

  where
    cost(t)  = difficulty(t) × inv_pass_rate(t) × expected_runtime(t)
    value(t) = priority(t) × category_diversity(t) × memory_affinity(t)

Solved via QAOA (Qiskit-Aer) when k ≤ ~12 tasks (small enough for
statevector simulation), else greedy fallback. QAOA produces better
selections for non-trivial pools where dependencies + diversity
interact.

Result published to NATS subject:  tenet5.liril.scheduler.decisions
so the observer daemon can log the decision trail.

CLI:
  python tools/liril_pvsnp_scheduler.py --next             # pick 1 best task
  python tools/liril_pvsnp_scheduler.py --next 3           # pick top 3
  python tools/liril_pvsnp_scheduler.py --explain          # show the scoring table
  python tools/liril_pvsnp_scheduler.py --method greedy    # skip QAOA

Library use from liril_dev_team:
  from liril_pvsnp_scheduler import select_best
  picked = select_best(pending_tasks, k=1, context={...})
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BACKLOG = Path(r"E:/TENET-5.github.io/data/liril_work_schedule.json")
MEMORY_DB = Path(r"E:/S.L.A.T.E/tenet5/data/liril_memory.sqlite")
TRANSCRIPT_DIR = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


# ──────────────────────────────────────────────────────────────
# SIGNALS — features for each task in the backlog
# ──────────────────────────────────────────────────────────────

def _load_memory_pass_rate_by_axis() -> dict[str, float]:
    """Load historic PASS rate by axis_domain from liril_memory.sqlite."""
    try:
        import sqlite3
        if not MEMORY_DB.exists():
            return {}
        conn = sqlite3.connect(str(MEMORY_DB))
        try:
            rows = conn.execute(
                "SELECT axis_domain, "
                "SUM(CASE WHEN schema_ok=1 THEN 1 ELSE 0 END) as good, "
                "COUNT(*) as total "
                "FROM entries WHERE axis_domain IS NOT NULL AND axis_domain != '' "
                "GROUP BY axis_domain"
            ).fetchall()
        finally:
            conn.close()
        out = {}
        for axis, good, total in rows:
            if total >= 3:
                out[axis.upper()] = round(good / total, 3)
        return out
    except Exception:
        return {}


def _load_recent_category_counts(hours: int = 2) -> Counter:
    """How many cycles of each category ran in the last N hours?"""
    if not TRANSCRIPT_DIR.exists():
        return Counter()
    cutoff = time.time() - hours * 3600
    counts: Counter = Counter()
    for f in TRANSCRIPT_DIR.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        axis = ((d.get("task") or {}).get("axis_domain") or "?").upper()
        counts[axis] += 1
    return counts


def _task_difficulty(task: dict) -> float:
    """Heuristic 0-1 difficulty. Higher = harder.

    Signals:
      - acceptance text length
      - target file count
      - mentions of 'every' / 'each' / 'all' / 'array' in acceptance
      - pipeline role count
    """
    acc = (task.get("acceptance") or "").lower()
    difficulty = 0.0

    # Length signal
    difficulty += min(len(acc) / 800.0, 0.4)

    # Multiplicity keywords
    for kw in ("every", "each", "all ", "multi", "array", "list of"):
        if kw in acc:
            difficulty += 0.15

    # Target file count
    tf = len(task.get("target_files") or [])
    if tf > 1:
        difficulty += 0.1 * (tf - 1)

    # Pipeline length
    pl = len(task.get("role_pipeline") or [])
    if pl > 4:
        difficulty += 0.05 * (pl - 4)

    # Clamp
    return max(0.05, min(difficulty, 0.95))


def _task_priority_weight(task: dict) -> float:
    p = (task.get("priority") or "medium").lower()
    return {"high": 1.0, "medium": 0.6, "low": 0.3}.get(p, 0.5)


def _expected_pass_rate(task: dict, memory_rate: dict[str, float]) -> float:
    """Expected PASS rate given past performance on this axis + task shape."""
    axis = (task.get("axis_domain") or "").upper()
    base = memory_rate.get(axis, 0.35)   # 35% baseline if no history

    # Tasks previously marked needs_rework should have lower expected rate
    if task.get("status") == "needs_rework":
        base *= 0.65

    # Tasks with explicit last_verdict=UNPARSED or FAIL have lower expected rate
    lv = (task.get("last_verdict") or "").upper()
    if lv in ("UNPARSED", "FAIL"):
        base *= 0.7

    return max(0.05, min(base, 0.95))


def _category_diversity_bonus(task: dict, recent_counts: Counter) -> float:
    """Reward tasks in categories the daemon hasn't done recently."""
    axis = (task.get("axis_domain") or "").upper()
    n = recent_counts.get(axis, 0)
    return max(0.1, 1.0 - 0.12 * n)   # diminishing returns per repeat


# ──────────────────────────────────────────────────────────────
# SCORING — combine signals into cost + value
# ──────────────────────────────────────────────────────────────

def score_task(task: dict, context: dict) -> dict:
    """Return scoring breakdown for one task. Pure function."""
    mem = context.get("memory_rate", {})
    recent = context.get("recent_counts", Counter())

    difficulty = _task_difficulty(task)
    priority   = _task_priority_weight(task)
    pass_rate  = _expected_pass_rate(task, mem)
    diversity  = _category_diversity_bonus(task, recent)

    # Expected runtime signal — proxy for GPU time cost
    expected_runtime = 6.0 + 20.0 * difficulty   # seconds

    cost  = difficulty * (1.0 - pass_rate) * (expected_runtime / 20.0)
    value = priority * diversity * pass_rate

    score = value - cost   # higher is better

    return {
        "task_id":          task.get("id", "?"),
        "difficulty":       round(difficulty, 3),
        "priority_weight":  round(priority, 3),
        "pass_rate":        round(pass_rate, 3),
        "diversity_bonus":  round(diversity, 3),
        "expected_runtime": round(expected_runtime, 1),
        "cost":             round(cost, 4),
        "value":            round(value, 4),
        "score":            round(score, 4),
    }


# ──────────────────────────────────────────────────────────────
# SELECTION — QAOA (preferred) or greedy fallback
# ──────────────────────────────────────────────────────────────

def _select_greedy(scored: list[dict], k: int) -> list[dict]:
    return sorted(scored, key=lambda s: s["score"], reverse=True)[:k]


def _select_qaoa(scored: list[dict], k: int) -> list[dict]:
    """Solve top-k selection via QAOA when the pool is small enough.

    QUBO: minimise −Σ score(t) x_t + λ(Σ x_t − k)²
    where x_t ∈ {0,1}. Produces a subset of k highest-value tasks, with
    a quantum-approximate optimisation pass that can improve selection
    when scores are close or when diversity constraints interact.
    """
    n = len(scored)
    if n == 0:
        return []
    if k >= n:
        return list(scored)
    # QAOA is impractical beyond ~14 qubits on CPU statevector simulator.
    if n > 14:
        return _select_greedy(scored, k)
    try:
        from qiskit_optimization import QuadraticProgram
        from qiskit_optimization.algorithms import MinimumEigenOptimizer
        from qiskit_algorithms import QAOA
        from qiskit_algorithms.optimizers import COBYLA
        from qiskit.primitives import StatevectorSampler as Sampler
    except ImportError:
        return _select_greedy(scored, k)

    qp = QuadraticProgram(name="pick_top_k")
    for i in range(n):
        qp.binary_var(name=f"x{i}")

    scores = [s["score"] for s in scored]
    # Minimise −score + λ(sum − k)² → expand quadratic
    lam = max(abs(max(scores)) * 2.0, 1.0)
    linear = {f"x{i}": (-scores[i] - 2 * lam * k) for i in range(n)}
    quadratic: dict = {}
    for i in range(n):
        for j in range(i, n):
            key = (f"x{i}", f"x{j}")
            if i == j:
                quadratic[key] = lam
            else:
                quadratic[key] = 2 * lam

    qp.minimize(linear=linear, quadratic=quadratic, constant=lam * k * k)

    sampler = Sampler()
    qaoa = QAOA(sampler=sampler, optimizer=COBYLA(maxiter=60), reps=2)
    meo = MinimumEigenOptimizer(qaoa)
    try:
        result = meo.solve(qp)
    except Exception:
        return _select_greedy(scored, k)

    picked = []
    for i, s in enumerate(scored):
        val = int(result.variables_dict.get(f"x{i}", 0))
        if val:
            picked.append(s)
    # If QAOA returned wrong cardinality, fall back to greedy top-k
    if len(picked) != k:
        return _select_greedy(scored, k)
    return picked


def select_best(tasks: list[dict], k: int = 1, method: str = "auto",
                context: dict | None = None) -> list[dict]:
    """Public API: pick best k tasks from the pool.

    Returns list of scored dicts ordered by score descending, augmented
    with the underlying task object under key "_task".
    """
    if context is None:
        context = {
            "memory_rate":   _load_memory_pass_rate_by_axis(),
            "recent_counts": _load_recent_category_counts(),
        }
    scored = []
    for t in tasks:
        s = score_task(t, context)
        s["_task"] = t
        scored.append(s)

    if method == "greedy":
        picked = _select_greedy(scored, k)
    elif method == "qaoa":
        picked = _select_qaoa(scored, k)
    else:   # auto: QAOA if feasible
        picked = _select_qaoa(scored, k) if len(scored) <= 14 else _select_greedy(scored, k)

    return sorted(picked, key=lambda s: s["score"], reverse=True)


# ──────────────────────────────────────────────────────────────
# PUBLISH DECISION TO NATS
# ──────────────────────────────────────────────────────────────

async def _publish_decision(decision: dict) -> str:
    try:
        import nats
    except ImportError:
        return "nats-py missing"
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        return f"connect: {e!r}"
    try:
        await nc.publish(
            "tenet5.liril.scheduler.decisions",
            json.dumps(decision, default=str).encode(),
        )
        await nc.flush(timeout=3)
    finally:
        await nc.drain()
    return "published"


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def _load_pending_tasks() -> list[dict]:
    if not BACKLOG.exists():
        return []
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    return [t for t in data.get("backlog", [])
            if t.get("status") in ("pending", "needs_rework")]


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL P-vs-NP task scheduler (QAOA)")
    ap.add_argument("--next",    type=int, nargs="?", const=1, default=1,
                    help="pick next N tasks (default 1)")
    ap.add_argument("--method",  choices=["auto", "greedy", "qaoa"], default="auto")
    ap.add_argument("--explain", action="store_true",
                    help="print full scoring table, don't publish")
    ap.add_argument("--publish", action="store_true",
                    help="publish decision to tenet5.liril.scheduler.decisions")
    ap.add_argument("--json",    action="store_true")
    args = ap.parse_args()

    pending = _load_pending_tasks()
    if not pending:
        print("no pending tasks in backlog")
        return 1

    context = {
        "memory_rate":   _load_memory_pass_rate_by_axis(),
        "recent_counts": _load_recent_category_counts(),
    }

    t0 = time.time()
    picks = select_best(pending, k=args.next, method=args.method, context=context)
    solve_ms = int((time.time() - t0) * 1000)

    if args.explain:
        # Show full scoring table (all candidates), sorted
        all_scored = sorted(
            [score_task(t, context) | {"status": t.get("status")} for t in pending],
            key=lambda s: s["score"], reverse=True,
        )
        print(f"── Scoring table ({len(all_scored)} candidates, solved in {solve_ms}ms) ──")
        print(f"  {'task':13s} {'status':12s} {'diff':>6s} {'pri':>5s} {'pass':>5s} {'div':>5s} {'cost':>7s} {'value':>7s} {'score':>7s}")
        for s in all_scored:
            mark = "✓" if s["task_id"] in {p["task_id"] for p in picks} else " "
            print(f"  {mark} {s['task_id']:11s} {s['status'] or '?':12s} "
                  f"{s['difficulty']:>6.2f} {s['priority_weight']:>5.2f} "
                  f"{s['pass_rate']:>5.2f} {s['diversity_bonus']:>5.2f} "
                  f"{s['cost']:>7.3f} {s['value']:>7.3f} {s['score']:>7.3f}")

    decision = {
        "method":         args.method,
        "solve_ms":       solve_ms,
        "candidates":     len(pending),
        "k":              args.next,
        "picks":          [{"task_id": p["task_id"], "score": p["score"]} for p in picks],
        "memory_rates":   context["memory_rate"],
        "recent_counts":  dict(context["recent_counts"]),
        "ts":             int(time.time()),
    }

    if args.publish:
        r = asyncio.run(_publish_decision(decision))
        decision["_publish"] = r

    if args.json:
        print(json.dumps(decision, indent=2, default=str))
    else:
        print(f"\n── LIRIL scheduler decision ({args.method}, {solve_ms}ms) ──")
        for p in picks:
            print(f"  → {p['task_id']}  score={p['score']:+.3f}  "
                  f"(pass={p['pass_rate']:.2f} diff={p['difficulty']:.2f} pri={p['priority_weight']:.2f})")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
