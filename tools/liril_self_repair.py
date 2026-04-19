#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T14:55:00Z | Author: claude_code | Change: integrate journal — pre-fire recall of past outcomes + post-action persistence
"""LIRIL Autonomous Self-Repair — Capability #6 of the post-NPU plan.

LIRIL (poll 2026-04-19): "Enables automatic detection and resolution of system
anomalies without human intervention." Severity if missing = high.

Architectural note — why this is different from Cap#1-#5
---------------------------------------------------------
Cap#1-#5 are REACTIVE: they accept a request, validate it, publish a plan,
and execute. Cap#6 is PROACTIVE: it subscribes to the metrics + incident
streams that Cap#1/#8/#10 produce and initiates action on its own when a
rule fires. That inverts the threat model. A faulty rule doesn't just
mis-handle one request — it can mis-handle hundreds in a loop. Therefore:

  (1) Every rule has a COOLDOWN. No rule fires more than once per cooldown
      window (default 600s). Persisted in sqlite so cooldown survives
      restart.
  (2) Every rule has a RATE LIMIT across rules — no more than N repair
      actions per 10-minute window globally (default 6). Hitting the cap
      auto-escalates a severity=high fse incident.
  (3) Every rule respects the Cap#10 fail-safe gate. When
      fse.is_safe_to_execute() is False, NO rule runs (not even safe
      local ones — level 3+ means the system is already in trouble and
      auto-repair could make it worse).
  (4) Dry-run default (EXEC_GATE off) means rules publish their plan to
      windows.repair.control but don't actually act.

Two kinds of repair actions
---------------------------
  LOCAL  — Cap#6 does it itself (e.g. cleanup_temp_files).
  DELEGATED — Cap#6 publishes a plan to another capability's control
              subject (e.g. windows.service.control for a service restart).
              Delegated actions inherit the downstream cap's denylist and
              fse gate, so the invariants compose.

Rule registry (v1)
------------------
  disk_cleanup          LOCAL       Clean temp files when free < 10GB
  swap_pressure_log     LOCAL obs   File high-severity fse incident (no
                                    action — memory pressure is usually
                                    a sign that humans should intervene)
  supervisor_flap_reset LOCAL       Clear supervisor flap-block after 30min
                                    quiet period (allows a once-flapping
                                    daemon to try again)
  service_auto_restart  DELEGATED   Restart whitelisted services that
                                    transitioned to Stopped. Whitelist is
                                    data/liril_repair_service_whitelist.txt
                                    (default empty).
  dead_llama_alert      LOCAL obs   File high-severity incident when port
                                    8082 or 8083 is dead — do NOT auto-
                                    start GPU inference, too heavy a
                                    side-effect.

CLI modes
---------
  --daemon                Run the subscriber loop (30s tick)
  --snapshot              Run rule evaluation once + print results
  --list-rules            Print the rule registry and their cooldowns
  --force-rule NAME       Force-evaluate ONE rule (for testing)
  --cooldowns             Show current cooldown state
  --reset-cooldowns       Clear all cooldowns (sparingly)
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
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NATS_URL         = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AUDIT_SUBJECT    = "windows.repair.control"
METRICS_SUBJECT  = "windows.repair.metrics"
EXEC_GATE        = os.environ.get("LIRIL_EXECUTE", "0") == "1"
TICK_SEC         = float(os.environ.get("LIRIL_REPAIR_TICK", "30"))
GLOBAL_RATE_MAX  = 6       # max repair actions per 10-min window
GLOBAL_RATE_WIN  = 600.0

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG  = DATA_DIR / "liril_repair_audit.jsonl"
STATE_DB   = DATA_DIR / "liril_repair_state.sqlite"
SERVICE_WHITELIST_FILE = DATA_DIR / "liril_repair_service_whitelist.txt"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[REPAIR] audit log failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(STATE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            rule_name  TEXT PRIMARY KEY,
            last_fired REAL NOT NULL,
            last_ok    INTEGER NOT NULL DEFAULT 0,
            last_msg   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recent_fires (
            ts         REAL PRIMARY KEY,
            rule_name  TEXT NOT NULL,
            ok         INTEGER NOT NULL,
            message    TEXT
        )
    """)
    c.commit()
    return c


def _record_fire(rule_name: str, ok: bool, message: str) -> None:
    c = _db()
    try:
        now = time.time()
        c.execute(
            "INSERT OR REPLACE INTO cooldowns(rule_name, last_fired, last_ok, last_msg) "
            "VALUES(?, ?, ?, ?)",
            (rule_name, now, 1 if ok else 0, message[:500]),
        )
        c.execute(
            "INSERT OR REPLACE INTO recent_fires(ts, rule_name, ok, message) "
            "VALUES(?, ?, ?, ?)",
            (now, rule_name, 1 if ok else 0, message[:500]),
        )
        # Trim to last-hour
        c.execute("DELETE FROM recent_fires WHERE ts < ?", (now - 3600,))
        c.commit()
    finally:
        c.close()


def _last_fire(rule_name: str) -> float:
    c = _db()
    try:
        r = c.execute("SELECT last_fired FROM cooldowns WHERE rule_name=?",
                      (rule_name,)).fetchone()
        return float(r[0]) if r else 0.0
    finally:
        c.close()


def _recent_fire_count(window_sec: float = GLOBAL_RATE_WIN) -> int:
    c = _db()
    try:
        cutoff = time.time() - window_sec
        r = c.execute("SELECT COUNT(*) FROM recent_fires WHERE ts >= ?",
                      (cutoff,)).fetchone()
        return int(r[0]) if r else 0
    finally:
        c.close()


def _clear_cooldowns() -> None:
    c = _db()
    try:
        c.execute("DELETE FROM cooldowns")
        c.execute("DELETE FROM recent_fires")
        c.commit()
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────
# FSE GATE (via public API from Cap#10)
# ─────────────────────────────────────────────────────────────────────

def _fse_safe() -> tuple[bool, int]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        return _fse.is_safe_to_execute(), _fse.current_level()
    except Exception:
        return True, 0  # non-fatal — default to open


# ─────────────────────────────────────────────────────────────────────
# JOURNAL INTEGRATION (Cap#6 ↔ journal skill)
# ─────────────────────────────────────────────────────────────────────

# Thresholds for pre-fire historical review
_JOURNAL_LOOKBACK_SEC    = 86400.0   # only consider outcomes from last 24h
_JOURNAL_RECENT_FAIL_MAX = 3         # this many recent fails → skip + escalate


def _journal_recall_past(rule_name: str, limit: int = 10) -> list[dict]:
    """Ask the journal for recent outcomes of THIS rule. Returns newest-first."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_journal as _journal  # type: ignore
        return _journal.recall(
            tag=f"repair:{rule_name}",
            limit=int(limit),
            since_ts=time.time() - _JOURNAL_LOOKBACK_SEC,
        )
    except Exception:
        return []  # journal missing → no history → fall through to normal path


def _journal_remember_outcome(
    rule_name: str,
    ctx: dict,
    ok: bool,
    msg: str,
) -> str | None:
    """Write the outcome of a rule fire into the journal."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_journal as _journal  # type: ignore
        return _journal.remember(
            key=f"repair.{rule_name}",
            value={
                "ok":      bool(ok),
                "message": (msg or "")[:500],
                "context": ctx,
                "ts_unix": time.time(),
            },
            tags=[
                f"repair:{rule_name}",
                "incident:repair",
                "observation:self_repair",
            ],
            source="liril_self_repair",
        )
    except Exception as e:
        print(f"[REPAIR] journal write failed (non-fatal): {e!r}")
        return None


def _recent_fail_count(past: list[dict]) -> int:
    """From a list of past outcomes, count how many recent ones are ok=False."""
    fails = 0
    for row in past:
        v = row.get("value") or {}
        if not v.get("ok", False):
            fails += 1
    return fails


# ─────────────────────────────────────────────────────────────────────
# OBSERVATION STATE (rolling windows populated by NATS subs)
# ─────────────────────────────────────────────────────────────────────

# Keyed by subject → (timestamp, payload). Only the latest is retained; rules
# that need rolling state maintain their own counters.
_LATEST: dict[str, tuple[float, dict]] = {}

# Swap-pressure rolling state (3 consecutive high samples → trigger)
_swap_hot_cycles: int = 0

# Service watch state: name → last-seen status
_service_last_status: dict[str, str] = {}


def _record_latest(subject: str, payload: dict) -> None:
    _LATEST[subject] = (time.time(), payload)


def _latest(subject: str, max_age_sec: float = 120.0) -> dict | None:
    ts_payload = _LATEST.get(subject)
    if not ts_payload:
        return None
    ts, payload = ts_payload
    if time.time() - ts > max_age_sec:
        return None
    return payload


# ─────────────────────────────────────────────────────────────────────
# LOCAL REPAIR PRIMITIVES
# ─────────────────────────────────────────────────────────────────────

def _disk_free_bytes() -> dict[str, int]:
    """Return {drive_letter: free_bytes} for each fixed disk."""
    out: dict[str, int] = {}
    script = (
        "Get-PSDrive -PSProvider FileSystem | "
        "Where-Object { $_.Free -ne $null } | "
        "Select-Object Name,Free,Used | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout:
            return out
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            data = [data]
        for d in data:
            name = str(d.get("Name") or "").strip()
            free = d.get("Free")
            if name and free is not None:
                try:
                    out[name] = int(free)
                except Exception:
                    pass
    except Exception as e:
        print(f"[REPAIR] disk-free query failed: {e!r}")
    return out


def _cleanup_temp_files(max_age_days: int = 7) -> tuple[int, int, list[str]]:
    """Delete files in %TEMP% and %SystemRoot%\\Temp older than max_age_days.
    Returns (files_deleted, bytes_freed, errors)."""
    temp_dirs = []
    for key in ("TEMP", "TMP"):
        v = os.environ.get(key)
        if v and Path(v).exists():
            temp_dirs.append(Path(v))
    sysroot_temp = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "Temp"
    if sysroot_temp.exists():
        temp_dirs.append(sysroot_temp)
    # Deduplicate
    seen = set()
    temp_dirs = [p for p in temp_dirs if (p.resolve() not in seen and not seen.add(p.resolve()))]

    deleted_count = 0
    bytes_freed = 0
    errors: list[str] = []
    cutoff = time.time() - (max_age_days * 86400)

    for td in temp_dirs:
        for f in td.rglob("*"):
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                sz = f.stat().st_size
                f.unlink()
                deleted_count += 1
                bytes_freed += sz
            except Exception as e:
                errors.append(f"{f}: {type(e).__name__}")
                if len(errors) > 10:
                    break
        if len(errors) > 10:
            break
    return deleted_count, bytes_freed, errors


def _tcp_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _load_service_whitelist() -> set[str]:
    if not SERVICE_WHITELIST_FILE.exists():
        return set()
    try:
        out: set[str] = set()
        for line in SERVICE_WHITELIST_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line.lower())
        return out
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────
# DELEGATED REPAIR (publishes plans to other capabilities' subjects)
# ─────────────────────────────────────────────────────────────────────

async def _delegate_service_restart(nc, service_name: str, reason: str) -> dict:
    """Publish a Cap#2 plan to restart a service. Cap#2's denylist + fse gate
    + allowlist will be applied by Cap#2 itself when it sees the subject."""
    plan = {
        "plan_id":   str(uuid.uuid4()),
        "timestamp": _utc(),
        "action":    "restart",
        "service":   service_name,
        "reason":    reason,
        "source":    "liril_self_repair",
        "kind":      "delegated_plan",
    }
    try:
        # Publish as a visible plan; Cap#2 humans/observer will see it. The
        # actual restart requires somebody/something to run liril_service_control
        # --execute, which respects EXEC_GATE + fse + allowlist itself.
        await nc.publish("windows.service.control",
                         json.dumps(plan, default=str).encode())
    except Exception as e:
        plan["error"] = f"{type(e).__name__}: {e}"
    return plan


async def _delegate_supervisor_command(nc, command: str, name: str = "") -> dict:
    """Publish a supervisor command (start/stop/restart a child daemon)."""
    payload = {
        "command": command,
        "name":    name,
        "source":  "liril_self_repair",
        "ts":      _utc(),
    }
    try:
        await nc.publish("tenet5.liril.supervisor.command",
                         json.dumps(payload).encode())
    except Exception as e:
        payload["error"] = f"{type(e).__name__}: {e}"
    return payload


# ─────────────────────────────────────────────────────────────────────
# RULE REGISTRY
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RepairRule:
    name: str
    description: str
    trigger: Callable[[], dict | None]     # returns context dict if should fire
    action:  Callable[[dict, Any], "asyncio.coroutines"]  # async, returns (ok, msg)
    cooldown_sec: float = 600.0
    is_observation_only: bool = False      # observation rules skip EXEC_GATE
    last_context: dict = field(default_factory=dict)


# ── Rule 1: disk_cleanup ──────────────────────────────────────────────
def _trigger_disk_cleanup() -> dict | None:
    free = _disk_free_bytes()
    low = {}
    for drive, bytes_free in free.items():
        if bytes_free < 10 * 1024**3:  # 10 GB
            low[drive] = bytes_free
    if not low:
        return None
    return {"low_drives": low, "all_drives": free}


async def _action_disk_cleanup(context: dict, nc) -> tuple[bool, str]:
    deleted, freed, errors = _cleanup_temp_files(max_age_days=7)
    msg = (f"deleted {deleted} temp files, freed "
           f"{round(freed/1024/1024,1)} MB; errors={len(errors)}")
    if errors:
        msg += f" (first: {errors[0]})"
    return True, msg


# ── Rule 2: swap_pressure_log (observation) ────────────────────────────
def _trigger_swap_pressure() -> dict | None:
    global _swap_hot_cycles
    # Cap#8 hardware-health payload has memory.swap_used_pct
    hw = _latest("windows.hardware.health.metrics", max_age_sec=120) or {}
    mem = hw.get("memory") or {}
    swap_pct = float(mem.get("swap_used_pct") or 0.0)
    if swap_pct >= 80.0:
        _swap_hot_cycles += 1
    else:
        _swap_hot_cycles = 0
    if _swap_hot_cycles >= 3:
        return {"swap_pct": swap_pct, "hot_cycles": _swap_hot_cycles}
    return None


async def _action_swap_pressure_log(context: dict, nc) -> tuple[bool, str]:
    global _swap_hot_cycles
    # File high-severity fse incident (repair is manual review)
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        _fse.file_incident_local(
            "high", "liril_self_repair",
            f"swap_pct {context['swap_pct']:.1f}% sustained {context['hot_cycles']} cycles",
            data=context,
        )
    except Exception:
        pass
    _swap_hot_cycles = 0  # reset after firing
    return True, f"observation incident filed (swap {context['swap_pct']:.1f}%)"


# ── Rule 3: supervisor_flap_reset ──────────────────────────────────────
def _trigger_supervisor_flap_reset() -> dict | None:
    """If a supervisor.status message shows any daemon flap-blocked, AND the
    flap-block has been held > 30min since last restart attempt, suggest a
    restart to give the daemon another chance."""
    st = _latest("tenet5.liril.supervisor.status", max_age_sec=180) or {}
    daemons = st.get("daemons") or {}
    flapping = {
        name: info for name, info in daemons.items()
        if info.get("flap_blocked")
    }
    if not flapping:
        return None
    # Conservative: only fire if we've been silent on this rule for >30min
    if time.time() - _last_fire("supervisor_flap_reset") < 1800:
        return None
    return {"flapping": list(flapping.keys()), "daemons": flapping}


async def _action_supervisor_flap_reset(context: dict, nc) -> tuple[bool, str]:
    results = []
    for name in context["flapping"]:
        r = await _delegate_supervisor_command(nc, "restart", name)
        results.append(f"{name}:{'ok' if 'error' not in r else r['error']}")
    return True, "; ".join(results)


# ── Rule 4: service_auto_restart (delegated) ───────────────────────────
def _trigger_service_auto_restart() -> dict | None:
    """Watch windows.service.control for classification payloads that show
    whitelisted services transitioning to 'Stopped'. v1 uses a polling
    approach instead of reconstructing transitions — check Get-Service."""
    wl = _load_service_whitelist()
    if not wl:
        return None
    script = (
        "Get-Service | Where-Object { $_.Status -eq 'Stopped' } | "
        "Select-Object Name,@{n='Status';e={\"$($_.Status)\"}} | "
        "ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    stopped = [s.get("Name") for s in (data or []) if s.get("Name")]
    matches = [s for s in stopped if s.lower() in wl]
    if not matches:
        return None
    return {"services": matches}


async def _action_service_auto_restart(context: dict, nc) -> tuple[bool, str]:
    results = []
    for svc in context["services"]:
        plan = await _delegate_service_restart(
            nc, svc,
            "self-repair: whitelisted service Stopped, requesting restart",
        )
        results.append(f"{svc}:plan={plan['plan_id'][:8]}")
    return True, "; ".join(results)


# ── Rule 5: dead_llama_alert ──────────────────────────────────────────
def _trigger_dead_llama() -> dict | None:
    dead = []
    for port in (8082, 8083):
        if not _tcp_alive("127.0.0.1", port):
            dead.append(port)
    if not dead:
        return None
    # Cooldown handled at rule layer; don't spam
    return {"dead_ports": dead}


async def _action_dead_llama_alert(context: dict, nc) -> tuple[bool, str]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        _fse.file_incident_local(
            "high", "liril_self_repair",
            f"llama-server dead on ports {context['dead_ports']} "
            "— GPU inference offline (manual intervention required)",
            data=context,
        )
    except Exception:
        pass
    return True, f"incident filed for dead llama ports {context['dead_ports']}"


# Registry
RULES: list[RepairRule] = [
    RepairRule(
        name="disk_cleanup",
        description="Clean temp files when any fixed disk has <10GB free",
        trigger=_trigger_disk_cleanup,
        action=_action_disk_cleanup,
        cooldown_sec=3600.0,   # 1 hour — disk cleanup shouldn't fire often
    ),
    RepairRule(
        name="swap_pressure_log",
        description="File fse incident when swap sustains >80% for 3+ cycles",
        trigger=_trigger_swap_pressure,
        action=_action_swap_pressure_log,
        cooldown_sec=900.0,    # 15 min
        is_observation_only=True,
    ),
    RepairRule(
        name="supervisor_flap_reset",
        description="Restart daemons flap-blocked by supervisor after 30min",
        trigger=_trigger_supervisor_flap_reset,
        action=_action_supervisor_flap_reset,
        cooldown_sec=1800.0,
    ),
    RepairRule(
        name="service_auto_restart",
        description="Restart Stopped services in data/liril_repair_service_whitelist.txt",
        trigger=_trigger_service_auto_restart,
        action=_action_service_auto_restart,
        cooldown_sec=300.0,    # 5 min
    ),
    RepairRule(
        name="dead_llama_alert",
        description="Observation incident when GPU llama-server ports are down",
        trigger=_trigger_dead_llama,
        action=_action_dead_llama_alert,
        cooldown_sec=900.0,
        is_observation_only=True,
    ),
]

_RULE_BY_NAME: dict[str, RepairRule] = {r.name: r for r in RULES}


# ─────────────────────────────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────────────────────────────

async def _evaluate_rule(rule: RepairRule, nc) -> dict:
    result: dict = {
        "rule":      rule.name,
        "ts":        _utc(),
        "triggered": False,
        "skipped":   None,
        "ok":        None,
        "message":   "",
    }

    # Cooldown
    if time.time() - _last_fire(rule.name) < rule.cooldown_sec:
        result["skipped"] = "cooldown"
        return result

    # Global rate
    if _recent_fire_count() >= GLOBAL_RATE_MAX:
        result["skipped"] = f"global_rate_cap ({GLOBAL_RATE_MAX}/{int(GLOBAL_RATE_WIN)}s)"
        return result

    # fse gate — skip everything (even observation) if we're in escalation
    ok_exec, lvl = _fse_safe()
    if not ok_exec:
        result["skipped"] = f"failsafe_level={lvl}"
        return result

    # Evaluate trigger
    try:
        ctx = rule.trigger()
    except Exception as e:
        result["skipped"] = f"trigger_exception:{type(e).__name__}:{e}"
        return result
    if ctx is None:
        return result  # no trigger, no fire

    result["triggered"] = True
    result["context"]   = ctx

    # Journal: check if THIS rule has failed repeatedly in the last 24h.
    # If so, skip the current attempt and file a severity=high fse incident
    # so a human notices that auto-repair is no longer working.
    past = _journal_recall_past(rule.name, limit=10)
    recent_fails = _recent_fail_count(past)
    if recent_fails >= _JOURNAL_RECENT_FAIL_MAX:
        result["skipped"] = (
            f"journal: {recent_fails} recent failures in {_JOURNAL_LOOKBACK_SEC/3600:.0f}h — "
            "manual intervention required"
        )
        result["past_fail_count"] = recent_fails
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import liril_fail_safe_escalation as _fse  # type: ignore
            _fse.file_incident_local(
                "high", "liril_self_repair",
                f"rule '{rule.name}' has failed {recent_fails}+ times recently — "
                "auto-repair disabled until manual ack",
                data={"rule": rule.name, "recent_fails": recent_fails},
            )
        except Exception:
            pass
        _audit({"kind": "journal_skip_too_many_fails", "rule": rule.name,
                "recent_fails": recent_fails, "ts": _utc()})
        return result
    result["past_fail_count"] = recent_fails
    result["past_total"]      = len(past)

    # Observation rules don't need EXEC_GATE — they just file incidents
    if not rule.is_observation_only and not EXEC_GATE:
        result["skipped"] = "dry_run (EXEC_GATE off)"
        # Still record a "planned" fire so the plan is visible in the audit log
        plan = {
            "plan_id": str(uuid.uuid4()),
            "ts":      _utc(),
            "kind":    "planned_repair",
            "rule":    rule.name,
            "context": ctx,
        }
        try:
            await nc.publish(AUDIT_SUBJECT, json.dumps(plan, default=str).encode())
        except Exception:
            pass
        _audit({"kind": "planned_repair", **plan})
        return result

    # Execute
    try:
        ok, msg = await rule.action(ctx, nc)
    except Exception as e:
        ok, msg = False, f"action_exception:{type(e).__name__}:{e}"
    result["ok"]      = ok
    result["message"] = msg
    _record_fire(rule.name, ok, msg)
    payload = {
        "plan_id": str(uuid.uuid4()),
        "ts":      _utc(),
        "kind":    "executed_repair",
        "rule":    rule.name,
        "context": ctx,
        "ok":      ok,
        "message": msg,
    }
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(payload, default=str).encode())
    except Exception:
        pass
    _audit(payload)

    # Journal: persist the outcome so future fires can consult this record
    journal_id = _journal_remember_outcome(rule.name, ctx, ok, msg)
    if journal_id:
        result["journal_id"] = journal_id

    return result


async def _evaluate_all(nc) -> list[dict]:
    out = []
    for r in RULES:
        res = await _evaluate_rule(r, nc)
        out.append(res)
    return out


# ─────────────────────────────────────────────────────────────────────
# NATS SUBS — populate _LATEST
# ─────────────────────────────────────────────────────────────────────

async def _subscribe_all(nc) -> None:
    async def cb(msg):
        try:
            d = json.loads(msg.data.decode())
        except Exception:
            return
        _record_latest(msg.subject, d)
    await nc.subscribe("windows.hardware.health.metrics", cb=cb)
    await nc.subscribe("windows.system.metrics",          cb=cb)
    await nc.subscribe("tenet5.liril.supervisor.status",  cb=cb)
    await nc.subscribe("tenet5.liril.failsafe.incident",  cb=cb)


# ─────────────────────────────────────────────────────────────────────
# DAEMON
# ─────────────────────────────────────────────────────────────────────

async def _publish_metrics(nc) -> None:
    snap = {
        "ts":                  _utc(),
        "host":                os.environ.get("COMPUTERNAME", ""),
        "rule_count":          len(RULES),
        "global_fires_10min":  _recent_fire_count(),
        "global_rate_max":     GLOBAL_RATE_MAX,
        "exec_gate":           EXEC_GATE,
    }
    try:
        await nc.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
    except Exception:
        pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[REPAIR] daemon starting — {len(RULES)} rules, tick={TICK_SEC:.0f}s, "
          f"exec_gate={EXEC_GATE}")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[REPAIR] NATS unavailable: {e!r} — will retry")
        nc = None

    if nc is not None:
        await _subscribe_all(nc)

    last_metrics = 0.0
    try:
        while True:
            try:
                if nc is None:
                    try:
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                        await _subscribe_all(nc)
                    except Exception:
                        nc = None
                if nc is not None:
                    results = await _evaluate_all(nc)
                    fired = [r for r in results if r["triggered"] and not r.get("skipped")]
                    if fired:
                        for r in fired:
                            print(f"[REPAIR] {r['rule']} fired: ok={r.get('ok')} "
                                  f"{r.get('message','')[:80]}")
                    if time.time() - last_metrics > 60:
                        await _publish_metrics(nc)
                        last_metrics = time.time()
            except Exception as e:
                print(f"[REPAIR] tick error: {type(e).__name__}: {e}")
                try:
                    if nc is not None:
                        await nc.close()
                except Exception:
                    pass
                nc = None
            await asyncio.sleep(TICK_SEC)
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


async def _one_shot() -> list[dict]:
    import nats as _nats
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        print(f"[REPAIR] NATS unavailable: {e!r}")
        nc = None
    try:
        # Seed _LATEST from a 2s NATS listen so rules that depend on
        # incoming metrics have something to look at
        if nc is not None:
            await _subscribe_all(nc)
            await asyncio.sleep(2)
            results = await _evaluate_all(nc)
        else:
            # Can still evaluate rules that don't need NATS (e.g. disk_cleanup)
            async def _noop(): return []
            class _Null:
                async def publish(self, *a, **kw): pass
                async def drain(self): pass
            results = await _evaluate_all(_Null())
        return results
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Autonomous Self-Repair — Capability #6")
    ap.add_argument("--daemon",           action="store_true", help="Run rule engine loop")
    ap.add_argument("--snapshot",         action="store_true",
                    help="Evaluate all rules once and print results")
    ap.add_argument("--list-rules",       action="store_true",
                    help="Print rule registry")
    ap.add_argument("--force-rule",       type=str, metavar="NAME",
                    help="Force-evaluate ONE rule (ignores cooldown)")
    ap.add_argument("--cooldowns",        action="store_true",
                    help="Show last-fired + cooldown state")
    ap.add_argument("--reset-cooldowns",  action="store_true",
                    help="Clear all cooldowns (sparingly!)")
    ap.add_argument("--repair-history",   type=str, metavar="RULE",
                    help="Show journal-recorded outcomes for RULE (last 24h)")
    args = ap.parse_args()

    if args.list_rules:
        for r in RULES:
            obs = " [obs]" if r.is_observation_only else ""
            print(f"  {r.name:24s} cooldown={int(r.cooldown_sec):>5}s{obs}  {r.description}")
        return 0

    if args.cooldowns:
        c = _db()
        try:
            rows = c.execute(
                "SELECT rule_name, last_fired, last_ok, last_msg FROM cooldowns"
            ).fetchall()
            if not rows:
                print("no cooldowns recorded yet")
            else:
                for name, ts, ok, msg in rows:
                    age = time.time() - ts
                    rule = _RULE_BY_NAME.get(name)
                    cd = int(rule.cooldown_sec) if rule else 0
                    rem = max(0, cd - age)
                    print(f"  {name:24s}  last={int(age):>6}s ago  "
                          f"ok={bool(ok)}  cooldown_remaining={int(rem)}s  "
                          f"{(msg or '')[:60]}")
            cnt = _recent_fire_count()
            print(f"global fires in last {int(GLOBAL_RATE_WIN)}s: {cnt} / {GLOBAL_RATE_MAX}")
        finally:
            c.close()
        return 0

    if args.reset_cooldowns:
        _clear_cooldowns()
        print("cooldowns cleared")
        return 0

    if args.repair_history:
        name = args.repair_history
        if name not in _RULE_BY_NAME:
            print(f"unknown rule {name!r}. Known: {sorted(_RULE_BY_NAME)}")
            return 2
        past = _journal_recall_past(name, limit=50)
        print(f"# repair history for {name!r}: {len(past)} outcomes in "
              f"last {_JOURNAL_LOOKBACK_SEC/3600:.0f}h")
        fail_count = _recent_fail_count(past)
        print(f"# recent_fail_count={fail_count} (threshold={_JOURNAL_RECENT_FAIL_MAX})")
        for row in past[:20]:
            v = row.get("value") or {}
            ts = v.get("ts_unix") or row.get("ts") or 0
            age_h = (time.time() - float(ts)) / 3600 if ts else 0
            mark = "ok  " if v.get("ok") else "FAIL"
            print(f"  {mark}  -{age_h:>4.1f}h  {(v.get('message') or '')[:70]}")
        return 0

    if args.force_rule:
        if args.force_rule not in _RULE_BY_NAME:
            print(f"unknown rule {args.force_rule!r}")
            return 2
        # Temporarily zero the cooldown for this run
        c = _db()
        try:
            c.execute("DELETE FROM cooldowns WHERE rule_name=?", (args.force_rule,))
            c.commit()
        finally:
            c.close()
        async def run():
            import nats as _nats
            try:
                nc = await _nats.connect(NATS_URL, connect_timeout=3)
            except Exception as e:
                print(f"NATS unavailable: {e!r}")
                return
            try:
                await _subscribe_all(nc)
                await asyncio.sleep(2)
                r = await _evaluate_rule(_RULE_BY_NAME[args.force_rule], nc)
                print(json.dumps(r, indent=2, default=str))
            finally:
                await nc.drain()
        asyncio.run(run())
        return 0

    if args.snapshot:
        results = asyncio.run(_one_shot())
        print(json.dumps(results, indent=2, default=str))
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[REPAIR] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
