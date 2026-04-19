#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T17:10:00Z | Author: claude_code | Change: Grok-review fix — per-job asyncio.Lock prevents overlap across ticks
"""LIRIL Autonomous — runs website-dev + OSINT cycles on behalf of the CEO.

CEO directive (2026-04-19):
"Ensure LIRIL is autonomously developing the website and collecting OSINT."

Spec LIRIL gave when polled:

    DAEMON_NAME:       liril_autonomous
    OSINT_FETCH_MIN:   60    (hourly)
    WEBSITE_DEV_MIN:   120   (every 2h)
    GIT_COMMIT_RATE:   5     (max commits per hour, across all jobs)
    DRY_RUN_DEFAULT:   yes
    FSE_GATE:          yes   (override LIRIL's "no" — auto-repair and
                              git commits should both respect level >= 3)
    INTENT_GATE:       skip website-dev during MEETING (git churn during
                              a call is noisy); OSINT-only during MEETING.
    NATS_METRICS:      tenet5.liril.autonomous.metrics
    NATS_AUDIT:        tenet5.liril.autonomous.audit

Architecture
------------
Tick loop (every 60s):
  1. Read fse level (skip everything if >= 3).
  2. Read intent; route jobs accordingly (see INTENT_GATE above).
  3. For each registered JOB, if its cadence elapsed since last run,
     AND the global commit-rate cap hasn't been hit, AND the job's
     own kind-specific gates pass, execute it.
  4. Every job invocation journals a start event + end event (or
     exception). Journal tags:
         observation:autonomous, job:<name>, kind:osint|website
  5. Publish a metrics snapshot once per minute.

All jobs run as subprocesses of scripts that already exist (in the
TENET5 repo or the E:/TENET-5.github.io repo). This daemon is pure
orchestration — no business logic of its own — so it can be extended
just by adding entries to JOBS.

Git safety
----------
 * Never uses --no-verify, --no-gpg-sign, or any hook-skipping flags.
 * Never force-pushes.
 * Never pushes automatically. Commits are local; Daniel or a
   separate push daemon can choose when to deploy.
 * Global rate: max 5 commits per hour (sliding window, sqlite-backed
   via the journal). Hitting the cap pauses git-producing jobs for the
   rest of that window.

CLI
---
  --daemon            run the tick loop (supervisor mode)
  --run-once JOB      run one specific job immediately, once
  --list-jobs         show registry
  --last              print the last N journal entries for this daemon
  --dry-run           force dry-run for this invocation
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT       = Path(__file__).resolve().parent.parent
PY         = str(ROOT / ".venv" / "Scripts" / "python.exe")
NATS_URL   = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
METRICS_SUBJECT = "tenet5.liril.autonomous.metrics"
AUDIT_SUBJECT   = "tenet5.liril.autonomous.audit"

SLATE_ROOT   = ROOT.parent                         # E:/S.L.A.T.E
WEBSITE_ROOT = SLATE_ROOT.parent / "TENET-5.github.io"  # E:/TENET-5.github.io

TICK_SEC               = 60.0
GIT_COMMIT_RATE_CAP    = 5    # per 60 min
GIT_COMMIT_WINDOW_SEC  = 3600.0
DRY_RUN_DEFAULT        = os.environ.get("LIRIL_AUTO_DRY_RUN", "1") == "1"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# GATES — all imports are lazy and ImportError-tolerant
# ─────────────────────────────────────────────────────────────────────

def _fse_safe() -> tuple[bool, int]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        return _fse.is_safe_to_execute(), _fse.current_level()
    except Exception:
        return True, 0


def _intent() -> str:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_user_intent as _intent  # type: ignore
        return (_intent.current_intent().get("category") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _journal_remember(key: str, value: dict, tags: list[str]) -> str | None:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_journal as _j  # type: ignore
        return _j.remember(
            key=key, value=value,
            tags=tags,
            source="liril_autonomous",
        )
    except Exception as e:
        print(f"[AUTO] journal write failed (non-fatal): {e!r}")
        return None


def _journal_recall(tag: str, since_sec: float, limit: int = 50) -> list[dict]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_journal as _j  # type: ignore
        return _j.recall(tag=tag,
                         since_ts=time.time() - since_sec,
                         limit=int(limit))
    except Exception:
        return []


def _recent_commit_count() -> int:
    rows = _journal_recall(tag="autonomous_commit",
                           since_sec=GIT_COMMIT_WINDOW_SEC,
                           limit=100)
    return len(rows)


# ─────────────────────────────────────────────────────────────────────
# JOB DEFINITIONS
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    name: str
    kind: str                    # "osint" | "website"
    cadence_min: float           # minutes between runs
    runner: Callable             # (dry_run:bool) -> dict
    # Optional filter — return False to skip this job in the current tick
    guard: Callable | None = None
    # Per-job rolling state
    last_run_ts: float = 0.0
    last_result: dict = field(default_factory=dict)


# ── OSINT runners ─────────────────────────────────────────────────────

def _run_subprocess(cwd: Path, script: Path, args: list[str],
                    timeout: int = 300,
                    dry_run: bool = False) -> dict:
    """Run a Python script in a subprocess under the TENET5 venv."""
    if dry_run:
        return {"dry_run": True, "cmd": [str(script), *args]}
    if not script.exists():
        return {"ok": False, "error": f"script missing: {script}"}
    cmd = [PY, "-X", "utf8", str(script), *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = (f"{SLATE_ROOT / 'tenet5' / 'src'};{SLATE_ROOT};"
                          f"{cwd};{cwd / 'tools'}")
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), env=env,
            capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return {
            "ok":          r.returncode == 0,
            "rc":          r.returncode,
            "duration_s":  round(time.time() - t0, 2),
            "stdout_tail": (r.stdout or "")[-800:],
            "stderr_tail": (r.stderr or "")[-400:],
            "cmd":         cmd,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 124, "error": "timeout",
                "duration_s": round(time.time() - t0, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def runner_gov_osint(dry_run: bool) -> dict:
    return _run_subprocess(
        cwd=WEBSITE_ROOT,
        script=WEBSITE_ROOT / "tools" / "gov_osint_gatherer.py",
        args=["--cycle"] if (WEBSITE_ROOT / "tools" / "gov_osint_gatherer.py").exists() else [],
        timeout=600,
        dry_run=dry_run,
    )


def runner_osint_manifest(dry_run: bool) -> dict:
    return _run_subprocess(
        cwd=WEBSITE_ROOT,
        script=WEBSITE_ROOT / "tools" / "build_osint_manifest.py",
        args=[],
        timeout=300,
        dry_run=dry_run,
    )


def runner_osint_stabilize(dry_run: bool) -> dict:
    return _run_subprocess(
        cwd=WEBSITE_ROOT,
        script=WEBSITE_ROOT / "tools" / "osint_continuous_stabilization.py",
        args=["--cycle"] if (WEBSITE_ROOT / "tools" / "osint_continuous_stabilization.py").exists() else [],
        timeout=300,
        dry_run=dry_run,
    )


# ── Website-dev runners ───────────────────────────────────────────────

_UNIFIED_CSS_TAG = (
    '  <!-- LIRIL UNIFIED THEME (auto-migration) — see LIRIL_UNIFICATION.md -->\n'
    '  <link rel="stylesheet" href="/css/liril-unified.css?v=1">\n'
)
_UNIFIED_JS_TAG = (
    '<!-- LIRIL BOOTSTRAP (auto-migration) — see LIRIL_UNIFICATION.md -->\n'
    '<script src="/js/liril-bootstrap.js?v=1" defer></script>\n'
)


def _pick_unmigrated_page() -> Path | None:
    """Return one HTML page that hasn't been migrated yet, or None if
    everything is migrated (or website repo is missing)."""
    if not WEBSITE_ROOT.exists():
        return None
    candidates = [p for p in WEBSITE_ROOT.glob("*.html")
                  if p.name not in ("404.html",)]
    random.shuffle(candidates)
    for p in candidates:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Already migrated?
        if "liril-unified.css" in txt and "liril-bootstrap.js" in txt:
            continue
        # Has the head + body we need?
        if "</body>" not in txt or "<head>" not in txt.lower():
            continue
        # Has a style.css link we can anchor to?
        if "style.css" not in txt:
            continue
        return p
    return None


def _migrate_page(page: Path, dry_run: bool) -> dict:
    """Additive migration of ONE page to the unified stack. Commits the
    change to the website repo with a clearly-labelled message.

    Returns a dict describing what happened."""
    try:
        txt = page.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"read: {e}"}

    # Insert CSS tag immediately before the first <link rel="stylesheet" href="style.css">
    css_anchor = re.search(r'(\s*<link rel="stylesheet" href="style\.css[^"]*">)',
                            txt)
    if not css_anchor:
        return {"ok": False, "error": "no style.css anchor"}

    # Check if unified already present (shouldn't happen; _pick_unmigrated filters)
    if "liril-unified.css" in txt or "liril-bootstrap.js" in txt:
        return {"ok": False, "error": "already migrated"}

    new_txt = (txt[:css_anchor.start()] + "\n" + _UNIFIED_CSS_TAG
               + txt[css_anchor.start():])

    # Insert JS tag immediately before </body>
    body_idx = new_txt.lower().rfind("</body>")
    if body_idx < 0:
        return {"ok": False, "error": "no </body>"}
    new_txt = new_txt[:body_idx] + _UNIFIED_JS_TAG + new_txt[body_idx:]

    if dry_run:
        return {"ok": True, "dry_run": True, "page": page.name,
                "added_bytes": len(new_txt) - len(txt)}

    # Write
    try:
        page.write_text(new_txt, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"write: {e}"}

    # Commit
    commit_msg = (
        f"autonomous(liril): migrate {page.name} onto unified stack "
        f"(+liril-unified.css +liril-bootstrap.js)\n\n"
        "Additive — no existing tags removed. See LIRIL_UNIFICATION.md.\n"
        "Author: liril_autonomous daemon"
    )
    git_result = _git_commit(
        cwd=WEBSITE_ROOT,
        files=[page.name],
        message=commit_msg,
    )
    return {
        "ok":     bool(git_result.get("ok")),
        "page":   page.name,
        "commit": git_result,
    }


def _git_commit(cwd: Path, files: list[str], message: str) -> dict:
    """Stage + commit in cwd. Never pushes. Never skips hooks."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "add", *files],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return {"ok": False, "stage": "add",
                    "stderr": (r.stderr or "")[:400]}
        r = subprocess.run(["git", "-C", str(cwd), "commit", "-m", message],
                           capture_output=True, text=True, timeout=45,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return {"ok": False, "stage": "commit",
                    "stderr": (r.stderr or "")[:400],
                    "stdout": (r.stdout or "")[:200]}
        # Capture the new sha
        rr = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=10,
                            encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)
        sha = (rr.stdout or "").strip()
        return {"ok": True, "sha": sha}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def runner_website_migrate(dry_run: bool) -> dict:
    if _recent_commit_count() >= GIT_COMMIT_RATE_CAP:
        return {"ok": False, "skipped": "commit_rate_cap"}
    page = _pick_unmigrated_page()
    if page is None:
        return {"ok": True, "skipped": "no unmigrated pages found"}
    r = _migrate_page(page, dry_run=dry_run)
    # Tag successful commits into the journal for rate-limit tracking
    if r.get("ok") and not r.get("dry_run"):
        _journal_remember(
            key=f"autonomous_commit.website.{page.name}",
            value={"page": page.name, "sha": (r.get("commit") or {}).get("sha"),
                   "ts": _utc()},
            tags=["autonomous_commit", "kind:website",
                  "observation:autonomous"],
        )
    return r


def runner_website_checker(dry_run: bool) -> dict:
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "liril_website_checker.py",
        args=[],
        timeout=180,
        dry_run=dry_run,
    )


# ── Guards ────────────────────────────────────────────────────────────

def guard_website_dev() -> bool:
    """Skip website-dev work during MEETING; proceed otherwise."""
    if _intent() == "MEETING":
        return False
    return True


def guard_osint() -> bool:
    """OSINT is always safe — even during MEETING, gathering is invisible."""
    return True


def guard_heavy_gpu() -> bool:
    """Heavy GPU jobs skip during GAMING + MEETING (both need smooth GPU)."""
    if _intent() in ("GAMING", "MEETING"):
        return False
    return True


def guard_always() -> bool:
    return True


# ── LIRIL training runners ────────────────────────────────────────────

def runner_liril_train_cycle(dry_run: bool) -> dict:
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "liril_batch_train.py",
        args=["--cycle"] if (ROOT / "tools" / "liril_batch_train.py").exists() else [],
        timeout=600,
        dry_run=dry_run,
    )


def runner_gpu_benchmark(dry_run: bool) -> dict:
    """Run the GPU benchmark to detect drift. Heavy — hourly cap."""
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "gpu_benchmark.py",
        args=["--quick"] if (ROOT / "tools" / "gpu_benchmark.py").exists() else [],
        timeout=600,
        dry_run=dry_run,
    )


# ── TENET5 OS / health runners ────────────────────────────────────────

def runner_prove_all_quick(dry_run: bool) -> dict:
    """Run the prove-all harness in --quick mode to detect regressions."""
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "liril_prove_all.py",
        args=["--quick", "--json"],
        timeout=120,
        dry_run=dry_run,
    )


def runner_hydrogen_check(dry_run: bool) -> dict:
    """SLATE hydrogen (H1 binary-atom) 13-integration diagnostic."""
    return _run_subprocess(
        cwd=SLATE_ROOT / "hydrogen",
        script=SLATE_ROOT / "hydrogen" / "hydrogen_runtime.py",
        args=["--check-all"],
        timeout=90,
        dry_run=dry_run,
    )


def runner_journal_vacuum(dry_run: bool) -> dict:
    """Call the journal's --vacuum CLI to enforce size cap + expire TTLs."""
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "liril_journal.py",
        args=["--vacuum"],
        timeout=60,
        dry_run=dry_run,
    )


def runner_fse_snapshot(dry_run: bool) -> dict:
    """Periodic fse --status snapshot into the journal for trend analysis."""
    return _run_subprocess(
        cwd=ROOT,
        script=ROOT / "tools" / "liril_fail_safe_escalation.py",
        args=["--status"],
        timeout=30,
        dry_run=dry_run,
    )


# ── Active-goal runner (advances liril_goals on a 10-min cadence) ─────

# Priority order (high → low). First match wins.
_GOAL_PRIORITY_ORDER = ("critical", "high", "medium", "med", "low", "")


def _goal_import():
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_goals as _g  # type: ignore
        return _g
    except Exception as e:
        print(f"[AUTO] liril_goals import failed: {e!r}")
        return None


def _pick_top_goal(goals: list[dict]) -> dict | None:
    if not goals:
        return None
    def _prio_key(g: dict) -> tuple:
        p = (g.get("priority") or "").strip().lower()
        try:
            idx = _GOAL_PRIORITY_ORDER.index(p)
        except ValueError:
            idx = len(_GOAL_PRIORITY_ORDER)  # unknown → lowest
        # Within same priority, oldest first (break ties fairly)
        return (idx, g.get("created_ts") or "")
    return sorted(goals, key=_prio_key)[0]


def runner_active_goals(dry_run: bool) -> dict:
    """Pick the top-priority OPEN goal and advance it by one step.

    Flow:
      1. list_open() from liril_goals
      2. Pick highest-priority (tie-break by oldest created_ts)
      3. If it has no plan: decompose it this tick (don't execute yet)
      4. Else if plan has unexecuted steps: run the next one
      5. Else auto-close the goal with outcome=done

    Dry-run still consults the journal but skips writes.
    """
    g = _goal_import()
    if g is None:
        return {"ok": False, "error": "liril_goals not importable"}

    try:
        open_goals = g.list_open()
    except Exception as e:
        return {"ok": False, "error": f"list_open: {type(e).__name__}: {e}"}

    if not open_goals:
        return {"ok": True, "skipped": "no open goals"}

    top = _pick_top_goal(open_goals)
    if top is None:
        return {"ok": True, "skipped": "no top goal selected"}
    goal_id = top.get("id")
    if not goal_id:
        return {"ok": False, "error": "top goal missing id"}

    plan = top.get("plan") or []
    steps_run = int((top.get("progress") or {}).get("steps_run") or 0)

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "goal_id": goal_id,
            "priority": top.get("priority"),
            "plan_len": len(plan),
            "steps_run": steps_run,
            "would_do": ("decompose" if not plan
                          else "run_step" if steps_run < len(plan)
                          else "close"),
        }

    # Decompose path
    if not plan:
        dr = g.decompose(goal_id)
        return {"ok": bool(dr.get("ok")), "goal_id": goal_id,
                "action": "decompose",
                "step_count": dr.get("step_count"),
                "plan_source": dr.get("plan_source"),
                "error": dr.get("error")}

    # Step path
    if steps_run < len(plan):
        sr = g.run_next_step(goal_id)
        if sr.get("skipped"):
            return {"ok": True, "goal_id": goal_id, "action": "step",
                    "skipped": sr.get("skipped"),
                    "step_idx": steps_run}
        return {"ok": bool(sr.get("ok")), "goal_id": goal_id,
                "action": "step",
                "step_idx": steps_run,
                "done": sr.get("done"),
                "duration_s": sr.get("duration_s"),
                "step": sr.get("step"),
                "result_ok": (sr.get("result") or {}).get("ok")}

    # Auto-close path
    cr = g.close(goal_id, outcome="done")
    return {"ok": bool(cr.get("ok")), "goal_id": goal_id,
            "action": "close", "outcome": "done"}


# ── Docker neural network runners ────────────────────────────────────

def runner_docker_health(dry_run: bool) -> dict:
    """Check every Docker container's health. Record unhealthy ones to the
    journal + fire fse incidents for 'unhealthy' state."""
    if dry_run:
        return {"dry_run": True, "cmd": "docker ps --format ..."}
    try:
        r = subprocess.run(
            ["docker", "ps", "--format",
             "{{.Names}}|{{.Status}}|{{.Image}}"],
            capture_output=True, timeout=20, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "rc": r.returncode,
                "stderr": (r.stderr or "")[:400]}
    containers = []
    unhealthy: list[str] = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, status, image = parts[0], parts[1], parts[2]
        containers.append({"name": name, "status": status, "image": image})
        if "(unhealthy)" in status:
            unhealthy.append(name)
    if unhealthy:
        # Fire severity=high fse incident
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import liril_fail_safe_escalation as _fse  # type: ignore
            _fse.file_incident_local(
                "high", "liril_autonomous",
                f"Docker unhealthy: {', '.join(unhealthy)}",
                data={"unhealthy": unhealthy, "all": containers[:20]},
            )
        except Exception:
            pass
    return {
        "ok":         True,
        "count":      len(containers),
        "unhealthy":  unhealthy,
        "containers": containers[:20],
    }


def runner_docker_logs_rotation(dry_run: bool) -> dict:
    """Scan Docker container logs sizes; flag any > 100MB for rotation.
    Read-only; doesn't actually rotate (requires admin)."""
    if dry_run:
        return {"dry_run": True, "cmd": "docker ps + inspect"}
    try:
        r = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, timeout=15, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "rc": r.returncode}
    ids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    fat = []
    for cid in ids[:20]:
        try:
            ii = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.Name}}|{{.LogPath}}", cid],
                capture_output=True, timeout=10, text=True,
                encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            continue
        line = (ii.stdout or "").strip()
        if "|" not in line:
            continue
        name, log_path = line.split("|", 1)
        try:
            sz = Path(log_path).stat().st_size
        except Exception:
            sz = 0
        if sz > 100 * 1024 * 1024:
            fat.append({"name": name, "size_mb": round(sz / 1024 / 1024, 1)})
    return {"ok": True, "fat_logs": fat, "checked": len(ids)}


# ── Registry ──────────────────────────────────────────────────────────
# Ordered roughly by cadence (shortest first). All jobs respect the
# fse gate + commit-rate cap via _tick().
JOBS: list[Job] = [
    # --- LIRIL / TENET5 OS health (fast, cheap) ---------------------
    Job("prove_all_quick",   "os",      60,  runner_prove_all_quick,  guard_always),
    Job("fse_snapshot",      "os",      30,  runner_fse_snapshot,     guard_always),
    Job("journal_vacuum",    "os",      360, runner_journal_vacuum,   guard_always),
    Job("hydrogen_check",    "os",      120, runner_hydrogen_check,   guard_always),

    # --- Docker neural network --------------------------------------
    Job("docker_health",     "docker",  15,  runner_docker_health,    guard_always),
    Job("docker_logs",       "docker",  720, runner_docker_logs_rotation, guard_always),

    # --- OSINT -------------------------------------------------------
    Job("osint_stabilize",   "osint",   60,  runner_osint_stabilize,  guard_osint),
    Job("gov_osint",         "osint",   60,  runner_gov_osint,        guard_osint),
    Job("osint_manifest",    "osint",   180, runner_osint_manifest,   guard_osint),

    # --- Website (commits to E:/TENET-5.github.io) ------------------
    Job("website_migrate",   "website", 120, runner_website_migrate,  guard_website_dev),
    Job("website_checker",   "website", 240, runner_website_checker,  guard_website_dev),

    # --- LIRIL training (GPU-heavy, intent-aware) -------------------
    Job("liril_train_cycle", "liril",   240, runner_liril_train_cycle, guard_heavy_gpu),
    Job("gpu_benchmark",     "liril",   1440, runner_gpu_benchmark,    guard_heavy_gpu),

    # --- Active-goal scheduler (LIRIL's AGI-direction loop) ---------
    # Every 10 min, pick the highest-priority open goal and advance it
    # one step (decompose → step → close). liril_goals already caps
    # STEPS_PER_HOUR=10, so a runaway goal can't burn through.
    Job("active_goals",      "liril",   10,  runner_active_goals,     guard_always),
]
_JOB_BY_NAME = {j.name: j for j in JOBS}


# ─────────────────────────────────────────────────────────────────────
# TICK
# ─────────────────────────────────────────────────────────────────────

_start_ts = time.time()
_tick_count = 0

# Grok-review fix (2026-04-19): per-job asyncio.Lock prevents the same
# job firing twice if a prior invocation is still running (e.g. a slow
# WU search holding up gov_osint for >60s while the next tick arrives).
_job_locks: dict[str, asyncio.Lock] = {}


def _get_job_lock(name: str) -> asyncio.Lock:
    lk = _job_locks.get(name)
    if lk is None:
        lk = asyncio.Lock()
        _job_locks[name] = lk
    return lk


async def _tick(nc, dry_run: bool) -> dict:
    global _tick_count
    _tick_count += 1
    now = time.time()

    ok_exec, lvl = _fse_safe()
    if not ok_exec:
        return {"skipped_all": f"fse_level={lvl}", "ts": _utc()}

    intent = _intent()
    commit_count_60m = _recent_commit_count()

    results: list[dict] = []
    for job in JOBS:
        elapsed = now - job.last_run_ts
        if elapsed < job.cadence_min * 60:
            continue
        # Guard
        if job.guard and not job.guard():
            results.append({"job": job.name, "skipped": "guard"})
            continue
        # Commit-rate cap (applies to website jobs that commit)
        if job.kind == "website" and commit_count_60m >= GIT_COMMIT_RATE_CAP:
            results.append({"job": job.name, "skipped": "commit_rate_cap"})
            continue

        # Grok-review fix (2026-04-19): per-job lock — if a prior
        # invocation of THIS job is still running (e.g. a 60s WU search
        # crossing a 60s tick boundary), skip rather than fire in parallel.
        job_lock = _get_job_lock(job.name)
        if job_lock.locked():
            results.append({"job": job.name, "skipped": "already_running"})
            continue

        # Journal start
        start_id = _journal_remember(
            key=f"autonomous.job.start.{job.name}",
            value={"job": job.name, "kind": job.kind, "ts": _utc()},
            tags=[f"job:{job.name}", f"kind:{job.kind}",
                  "observation:autonomous"],
        )

        # Run under the per-job lock
        async with job_lock:
            try:
                result = job.runner(dry_run)
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        job.last_run_ts = now
        job.last_result = result

        # Journal end
        _journal_remember(
            key=f"autonomous.job.end.{job.name}",
            value={"job": job.name, "kind": job.kind,
                   "result": {k: v for k, v in result.items()
                              if k not in ("stdout_tail", "stderr_tail")},
                   "ts": _utc()},
            tags=[f"job:{job.name}", f"kind:{job.kind}",
                  "observation:autonomous"],
        )

        results.append({"job": job.name, "result": result})

        # Publish audit
        if nc is not None:
            try:
                await nc.publish(AUDIT_SUBJECT, json.dumps({
                    "ts":     _utc(),
                    "job":    job.name,
                    "kind":   job.kind,
                    "result": {k: v for k, v in result.items()
                               if k not in ("stdout_tail", "stderr_tail")},
                }, default=str).encode())
            except Exception:
                pass

    return {
        "ts":                 _utc(),
        "tick":               _tick_count,
        "intent":             intent,
        "fse_level":          lvl,
        "commits_last_60m":   commit_count_60m,
        "jobs_run":           len(results),
        "jobs":               results,
    }


async def _publish_metrics(nc, snap: dict) -> None:
    if nc is None:
        return
    try:
        await nc.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
    except Exception:
        pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[AUTO] autonomous daemon starting — {len(JOBS)} jobs registered, "
          f"dry_run={DRY_RUN_DEFAULT}")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[AUTO] NATS unavailable: {e!r} — will retry each tick")
        nc = None

    last_metric_ts = 0.0
    try:
        while True:
            try:
                if nc is None:
                    try:
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                    except Exception:
                        nc = None
                snap = await _tick(nc, dry_run=DRY_RUN_DEFAULT)
                if snap.get("jobs_run"):
                    print(f"[AUTO] tick #{snap['tick']}: "
                          f"ran {snap['jobs_run']} jobs, "
                          f"intent={snap['intent']}, "
                          f"commits60m={snap['commits_last_60m']}")
                if time.time() - last_metric_ts > 60:
                    await _publish_metrics(nc, snap)
                    last_metric_ts = time.time()
            except Exception as e:
                print(f"[AUTO] tick error: {type(e).__name__}: {e}")
                try:
                    if nc is not None: await nc.close()
                except Exception: pass
                nc = None
            await asyncio.sleep(TICK_SEC)
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Autonomous — 24/7 OSINT + website development")
    ap.add_argument("--daemon",     action="store_true", help="Run the tick loop")
    ap.add_argument("--run-once",   type=str, metavar="JOB",
                    help="Run ONE registered job once and exit")
    ap.add_argument("--list-jobs",  action="store_true")
    ap.add_argument("--last",       type=int, nargs="?", const=10,
                    help="Show last N journal entries (default 10)")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Override environment; force dry-run for this invocation")
    args = ap.parse_args()

    if args.list_jobs:
        print(f"# registered jobs ({len(JOBS)})")
        print(f"# website_root: {WEBSITE_ROOT}")
        print(f"# commit_rate_cap: {GIT_COMMIT_RATE_CAP}/{int(GIT_COMMIT_WINDOW_SEC/60)}min")
        for j in JOBS:
            print(f"  {j.name:22s} kind={j.kind:7s} cadence={int(j.cadence_min):>4}min")
        return 0

    if args.last is not None:
        rows = _journal_recall("observation:autonomous",
                               since_sec=7 * 86400, limit=int(args.last))
        for r in rows[:int(args.last)]:
            ts = r.get("ts", 0)
            age_m = (time.time() - ts) / 60 if ts else 0
            v = r.get("value") or {}
            job = v.get("job", "?")
            kind = v.get("kind", "?")
            key = (r.get("key") or "").split(".")[-2:]
            print(f"  -{age_m:>5.0f}min  {kind:7s}  {'.'.join(key):30s}  "
                  f"{job[:30]}")
        return 0

    dry_run = args.dry_run or DRY_RUN_DEFAULT

    if args.run_once:
        if args.run_once not in _JOB_BY_NAME:
            print(f"unknown job {args.run_once!r}. Known: "
                  f"{sorted(_JOB_BY_NAME)}")
            return 2
        job = _JOB_BY_NAME[args.run_once]
        t0 = time.time()
        try:
            r = job.runner(dry_run=dry_run)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        r["elapsed_s"] = round(time.time() - t0, 2)
        print(json.dumps(r, indent=2, default=str)[:4000])
        return 0 if r.get("ok", True) else 1

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[AUTO] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
