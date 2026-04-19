#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T10:15:00Z | Author: claude_code | Change: wire fse.is_safe_to_execute() gate (Cap#10 enforcement)
"""LIRIL Windows Process Management — Capability #3 of the NPU-Domain plan.

LIRIL's priority ordering (from the NPU-Domain poll that shipped #1 and #2):
    1. Windows System Monitoring      ← tools/liril_windows_monitor.py   (shipped)
    2. Windows Service Control        ← tools/liril_service_control.py   (shipped)
    3. Windows Process Management     ← THIS FILE
    4. Windows Driver Management
    5. Windows Patch Management

Spec for #3 (obtained by direct Mistral-Nemo poll on 2026-04-19 because the
NemoServer container is currently misconfigured pointing at banned Ollama
ports 11434/11435; same model LIRIL dispatches for execute):

    CAPABILITY_3: Windows Process Management
    WHY:          Safe, auditable control over running Windows processes —
                  classify criticality, terminate/suspend/re-prioritise only
                  non-critical workloads, preserve system stability.
    MECHANISM:    NATS subjects 'windows.process.metrics' (snapshots) and
                  'windows.process.control' (plans/results); Windows process
                  API via PowerShell Get-Process + Stop-Process + taskkill;
                  NPU criticality classification via tenet5.liril.classify.
    FIRST_STEP:   tools/liril_process_manager.py — this file.
    SAFETY_PLAN:
        ALLOWLIST:       empty (user-curated, same pattern as Cap#2)
        DENYLIST:        hard-coded critical Windows processes whose
                         termination crashes / cripples the system.
        RISK_SCHEMA:     process_classification:<low|med|high|critical>,
                         confidence:<0..1>
        DRY_RUN_DEFAULT: yes
        AUDIT_SUBJECT:   windows.process.control

Threat model
------------
Identical to Cap#2: the attacker is a confidently-wrong LLM plan ("terminate
MsMpEng.exe because it showed 40% CPU" would disable antivirus). Therefore:
  (1) Every mutation publishes to windows.process.control BEFORE execution.
  (2) Default is dry-run: plan is logged + published, nothing runs.
  (3) Execution requires --execute AND env LIRIL_EXECUTE=1 AND not-denied
      AND PID still present after veto window.
  (4) Denylist is hard-coded (no remote bypass via NATS).
  (5) 3-second veto window on windows.process.control.veto with plan_id.
  (6) Mutations require PID (integer), not name. Ambiguous names like
      "svchost.exe" refuse to mutate — caller must pick a specific PID.

CLI modes
---------
  --list                     List all processes (name, pid, cpu%, mem_mb, denied)
  --classify PID_OR_NAME     Classify process criticality via NPU (name lookup
                             resolves to all live PIDs; one classification per PID)
  --classify-top N           Classify top-N processes by CPU + memory
  --classify-all             Classify every running process (can be slow)
  --plan ACTION PID          Build a plan and publish — DRY RUN.
                             ACTION ∈ {terminate, suspend, resume, priority:LEVEL}
  --execute ACTION PID       Execute the plan. Requires LIRIL_EXECUTE=1.
  --daemon                   Classify-top every N minutes, publish metrics snapshots
  --snapshot                 One-shot publish of windows.process.metrics + exit
  --show-denylist / --show-allowlist

Parallels to Cap#1 and Cap#2
----------------------------
  - Cap#1 (monitor): 30-s metric pulse. Cap#3 reuses that cadence for snapshots
    but keyed on process list rather than system counters.
  - Cap#2 (service): plan→publish→veto→execute. Cap#3 reuses the full pattern,
    but re-verifies PID presence after the veto window because processes can
    exit on their own inside 3 seconds.
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
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

NATS_URL       = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AUDIT_SUBJECT  = "windows.process.control"
VETO_SUBJECT   = "windows.process.control.veto"
METRICS_SUBJECT = "windows.process.metrics"
EXEC_GATE      = os.environ.get("LIRIL_EXECUTE", "0") == "1"
VETO_WINDOW_SEC = 3.0

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWLIST_FILE = DATA_DIR / "liril_process_allowlist.txt"
AUDIT_LOG      = DATA_DIR / "liril_process_control.jsonl"

VALID_ACTIONS = {"terminate", "suspend", "resume"}  # priority handled via prefix
VALID_PRIORITY_LEVELS = {"idle", "belownormal", "normal", "abovenormal", "high", "realtime"}

# ── DENYLIST ─────────────────────────────────────────────────────────
# Hard-coded. Matches against the process image name (lowercased, with or
# without .exe). PID 0 (System Idle) and PID 4 (System) are also always denied.
# The list favours false-positives: we would rather refuse a legitimate kill
# than ship code that can brick someone's machine.
_DENYLIST_NAMES = {
    # Kernel / session / init
    "system", "registry", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
    "fontdrvhost.exe",
    # Shell / compositor (killing these logs the user out or freezes the UI)
    "dwm.exe", "explorer.exe",
    # Service host — any single svchost.exe may be carrying DNS, DHCP,
    # Defender, etc. Killing by PID without knowing which services it hosts
    # is reckless, so we refuse it at this layer.
    "svchost.exe", "sihost.exe", "taskhostw.exe", "runtimebroker.exe",
    # Security / Defender
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "smartscreen.exe",
    # Trusted installer / servicing (breaking these leaves OS unpatchable)
    "trustedinstaller.exe", "tiworker.exe", "msiexec.exe",
    # NVIDIA GPU — TENET5's compute surface
    "nvcontainer.exe", "nvdisplay.container.exe", "nvvsvc.exe",
    "nvsphelper64.exe", "nvtelemetrycontainer.exe",
    # Intel NPU / audio
    "intelaiixserv.exe", "intelaudioservice.exe", "intelcphdcpsvc.exe",
    # Audit pipeline (LIRIL's own eyes)
    "wmiprvse.exe", "eventvwr.exe",
}


def _is_denied_by_name(image_name: str) -> bool:
    if not image_name:
        return True  # refuse unknown by default
    lower = image_name.strip().lower()
    if lower in _DENYLIST_NAMES:
        return True
    # Wildcards: anything starting with MsMp, WdNis, SecOp, etc. is defence.
    if lower.startswith(("msmp", "wdnis", "secop", "mssec", "windefend")):
        return True
    return False


def _is_denied(pid: int, image_name: str) -> bool:
    # PID 0 (Idle) and PID 4 (System) — always denied
    if pid in (0, 4):
        return True
    return _is_denied_by_name(image_name)


def _load_allowlist() -> set[str]:
    """User-curated allowlist. Lower-cased image names (with .exe)."""
    if not ALLOWLIST_FILE.exists():
        return set()
    try:
        raw = ALLOWLIST_FILE.read_text(encoding="utf-8", errors="replace")
        out: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.add(line.lower())
        return out
    except Exception:
        return set()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[PROC-MGR] audit log write failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# PROCESS DISCOVERY
# ─────────────────────────────────────────────────────────────────────

def _pwsh_json(script: str, timeout: int = 20) -> list | dict | None:
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    except Exception:
        return None


def list_processes() -> list[dict]:
    """Return a list of dicts: name, pid, cpu_s, working_set_mb, priority, threads.

    Prefers psutil (fast, accurate CPU%) but falls back to PowerShell Get-Process.
    """
    try:
        import psutil  # type: ignore
        out = []
        for p in psutil.process_iter(["pid", "name", "cpu_times",
                                      "memory_info", "num_threads", "nice"]):
            try:
                info = p.info
                mi = info.get("memory_info")
                ct = info.get("cpu_times")
                out.append({
                    "pid":             int(info.get("pid") or 0),
                    "name":            (info.get("name") or "").strip(),
                    "cpu_s":           float((ct.user + ct.system) if ct else 0.0),
                    "working_set_mb":  round((mi.rss / (1024 * 1024)) if mi else 0.0, 1),
                    "threads":         int(info.get("num_threads") or 0),
                    "priority":        info.get("nice"),
                })
            except Exception:
                continue
        return out
    except ImportError:
        pass

    # Fallback: PowerShell Get-Process
    data = _pwsh_json(
        "Get-Process | Select-Object "
        "Id,ProcessName,@{n='CPU';e={$_.CPU}},"
        "@{n='WS';e={[math]::Round($_.WorkingSet64/1MB,1)}},"
        "@{n='Threads';e={$_.Threads.Count}},"
        "@{n='Priority';e={\"$($_.PriorityClass)\"}} "
        "| ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for p in data:
        name = p.get("ProcessName") or ""
        if name and not name.lower().endswith(".exe"):
            name = name + ".exe"
        out.append({
            "pid":            int(p.get("Id") or 0),
            "name":           name,
            "cpu_s":          float(p.get("CPU") or 0.0),
            "working_set_mb": float(p.get("WS") or 0.0),
            "threads":        int(p.get("Threads") or 0),
            "priority":       p.get("Priority"),
        })
    return out


def process_state(pid: int) -> dict | None:
    """Return the current state of PID or None if it doesn't exist."""
    try:
        import psutil  # type: ignore
        try:
            p = psutil.Process(int(pid))
            with p.oneshot():
                ct = p.cpu_times()
                mi = p.memory_info()
                return {
                    "pid":            p.pid,
                    "name":           (p.name() or "").strip(),
                    "status":         p.status(),
                    "cpu_s":          float(ct.user + ct.system),
                    "working_set_mb": round(mi.rss / (1024 * 1024), 1),
                    "threads":        p.num_threads(),
                    "ppid":           p.ppid(),
                    "create_time":    p.create_time(),
                }
        except psutil.NoSuchProcess:
            return None
        except Exception:
            return None
    except ImportError:
        pass

    data = _pwsh_json(
        f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | "
        "Select-Object Id,ProcessName,@{n='CPU';e={$_.CPU}},"
        "@{n='WS';e={[math]::Round($_.WorkingSet64/1MB,1)}},"
        "@{n='Threads';e={$_.Threads.Count}},"
        "@{n='Priority';e={\"$($_.PriorityClass)\"}} "
        "| ConvertTo-Json -Compress"
    )
    if not isinstance(data, dict):
        return None
    name = data.get("ProcessName") or ""
    if name and not name.lower().endswith(".exe"):
        name = name + ".exe"
    return {
        "pid":            int(data.get("Id") or 0),
        "name":           name,
        "status":         "running",
        "cpu_s":          float(data.get("CPU") or 0.0),
        "working_set_mb": float(data.get("WS") or 0.0),
        "threads":        int(data.get("Threads") or 0),
        "priority":       data.get("Priority"),
    }


def resolve_pids_by_name(name: str) -> list[int]:
    """Return all PIDs whose image name matches (case-insensitive)."""
    want = (name or "").strip().lower()
    if not want:
        return []
    if not want.endswith(".exe"):
        want = want + ".exe"
    return [p["pid"] for p in list_processes()
            if (p["name"] or "").lower() == want and p["pid"] > 0]


# ─────────────────────────────────────────────────────────────────────
# NPU CLASSIFY — risk scoring via LIRIL
# ─────────────────────────────────────────────────────────────────────

async def _classify_via_npu(nc, proc: dict) -> dict:
    """Ask LIRIL (tenet5.liril.classify) to tag criticality for this process."""
    text = (
        f"Windows process: {proc.get('name','')} "
        f"(pid={proc.get('pid','')}, threads={proc.get('threads','')}, "
        f"mem_mb={proc.get('working_set_mb','')}, cpu_s={proc.get('cpu_s','')})"
    )
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text, "source": "process_manager"}).encode(),
            timeout=5,
        )
        d = json.loads(msg.data.decode())
        axis = d.get("domain") or d.get("axis")
        conf = d.get("confidence")
        return {
            "process_classification": _axis_to_risk(axis, proc.get("name") or ""),
            "confidence": conf,
            "axis": axis,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _axis_to_risk(axis: str | None, name: str) -> str:
    """Map LIRIL's axis onto the RISK_SCHEMA. Denylisted names are always 'critical'."""
    if _is_denied_by_name(name):
        return "critical"
    if not axis:
        return "unknown"
    a = axis.upper()
    if any(k in a for k in ("SECURITY", "NETWORK", "KERNEL", "OS")):
        return "critical"
    if any(k in a for k in ("ETHICS", "SURVEILLANCE", "IDENTITY")):
        return "high"
    if any(k in a for k in ("TECHNOLOGY", "COMPUTE", "DATA")):
        return "medium"
    return "low"


# ─────────────────────────────────────────────────────────────────────
# PLAN + EXECUTE
# ─────────────────────────────────────────────────────────────────────

_PRIORITY_RE = re.compile(r"^priority:(?P<level>[A-Za-z]+)$")


def _parse_action(raw: str) -> tuple[str, str | None]:
    """Parse 'terminate' / 'suspend' / 'resume' / 'priority:belownormal'.
    Returns (base_action, priority_level or None)."""
    if not raw:
        raise ValueError("action is empty")
    m = _PRIORITY_RE.match(raw)
    if m:
        lvl = m.group("level").lower()
        if lvl not in VALID_PRIORITY_LEVELS:
            raise ValueError(f"invalid priority level {lvl!r}; must be one of {sorted(VALID_PRIORITY_LEVELS)}")
        return "priority", lvl
    if raw in VALID_ACTIONS:
        return raw, None
    raise ValueError(f"unknown action {raw!r}; must be one of {sorted(VALID_ACTIONS)} or 'priority:LEVEL'")


def _make_plan(action_raw: str, pid: int, reason: str) -> dict:
    action, level = _parse_action(action_raw)
    state = process_state(pid) or {}
    name  = state.get("name", "") or "<unknown>"
    return {
        "plan_id":      str(uuid.uuid4()),
        "timestamp":    _utc(),
        "action":       action,                 # terminate | suspend | resume | priority
        "priority_level": level,                # only set when action == priority
        "pid":          int(pid),
        "image_name":   name,
        "reason":       reason,
        "denied":       _is_denied(pid, name),
        "allowed":      (name.lower() in _load_allowlist()) if name else False,
        "dry_run":      not EXEC_GATE,
        "pre_state":    state or None,
    }


async def _publish_plan(nc, plan: dict) -> None:
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(plan).encode())
        _audit({"kind": "plan_published", **plan})
    except Exception as e:
        print(f"[PROC-MGR] publish plan failed: {e!r}")


async def _wait_for_veto(nc, plan_id: str) -> dict | None:
    got: dict | None = None
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _cb(msg):
        nonlocal got
        if fut.done():
            return
        try:
            d = json.loads(msg.data.decode())
            if d.get("plan_id") == plan_id:
                got = d
                fut.set_result(True)
        except Exception:
            pass

    sub = await nc.subscribe(VETO_SUBJECT, cb=_cb)
    try:
        await asyncio.wait_for(fut, timeout=VETO_WINDOW_SEC)
    except asyncio.TimeoutError:
        pass
    finally:
        await sub.unsubscribe()
    return got


def _run_terminate(pid: int) -> tuple[bool, str]:
    try:
        import psutil  # type: ignore
        try:
            p = psutil.Process(int(pid))
            p.terminate()
            try:
                p.wait(timeout=3)
                return True, "terminated"
            except psutil.TimeoutExpired:
                p.kill()
                return True, "killed (force)"
        except psutil.NoSuchProcess:
            return False, "no such process"
        except psutil.AccessDenied as e:
            return False, f"access denied: {e}"
    except ImportError:
        pass
    # Fallback: taskkill
    try:
        r = subprocess.run(
            ["taskkill.exe", "/PID", str(int(pid)), "/F"],
            capture_output=True, timeout=10, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "non-zero exit").strip()[:500]
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _run_priority(pid: int, level: str) -> tuple[bool, str]:
    """Change priority class via PowerShell."""
    mapping = {
        "idle":        "Idle",
        "belownormal": "BelowNormal",
        "normal":      "Normal",
        "abovenormal": "AboveNormal",
        "high":        "High",
        "realtime":    "RealTime",  # dangerous; we block below
    }
    pc = mapping.get(level.lower())
    if not pc:
        return False, f"invalid priority {level!r}"
    if pc == "RealTime":
        return False, "refusing to set RealTime priority (system-destabilising)"
    cmd = (
        f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
        "if ($null -eq $p) { Write-Error 'no such process'; exit 1 } "
        f"$p.PriorityClass = '{pc}'; "
        "Write-Output 'ok'"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, timeout=10, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "non-zero exit").strip()[:500]
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _run_suspend_resume(pid: int, action: str) -> tuple[bool, str]:
    """Suspend/resume are plan-only in v1: Windows has no built-in cmdlet and
    shipping NtSuspendProcess bindings without review is out of scope for #3."""
    return False, (
        f"{action} is plan-only in v1 — install Sysinternals pssuspend.exe and "
        "run it by hand, or supply a signed binding in a follow-up capability"
    )


def _run_action(action: str, level: str | None, pid: int) -> tuple[bool, str]:
    if action == "terminate":
        return _run_terminate(pid)
    if action == "priority":
        return _run_priority(pid, level or "normal")
    if action in ("suspend", "resume"):
        return _run_suspend_resume(pid, action)
    return False, f"unknown action {action!r}"


async def do_action(action_raw: str, pid: int, reason: str = "") -> dict:
    """Plan → publish → veto-wait → re-check PID → (maybe) execute."""
    try:
        plan = _make_plan(action_raw, pid, reason or "no reason provided")
    except ValueError as e:
        return {"status": "invalid_action", "error": str(e)}

    if plan["denied"]:
        plan["status"] = "denied_by_denylist"
        _audit({"kind": "denied", **plan})
        return plan

    if not EXEC_GATE:
        plan["status"] = "dry_run_logged"
        _audit({"kind": "dry_run", **plan})
        return plan

    # Cap#10 fail-safe gate — refuse if the global escalation level restricts
    # mutations. Missing module is non-fatal.
    try:
        import liril_fail_safe_escalation as _fse
        if not _fse.is_safe_to_execute():
            plan["status"]         = "refused_by_failsafe"
            plan["failsafe_level"] = _fse.current_level()
            _audit({"kind": "refused_by_failsafe", **plan})
            return plan
    except ImportError:
        pass

    if not plan["allowed"]:
        plan["status"] = "not_in_allowlist"
        _audit({"kind": "blocked_not_allowed", **plan})
        return plan

    try:
        import nats as _nats
    except ImportError:
        plan["status"] = "nats_missing"
        return plan
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        plan["status"] = f"nats_connect_failed: {e!r}"
        return plan

    try:
        await _publish_plan(nc, plan)
        veto = await _wait_for_veto(nc, plan["plan_id"])
        if veto is not None:
            plan["status"] = "vetoed"
            plan["veto"]   = veto
            _audit({"kind": "vetoed", **plan})
            return plan

        # Re-verify PID still exists after veto window (process may have exited)
        recheck = process_state(plan["pid"])
        if recheck is None:
            plan["status"] = "pid_gone_before_execute"
            _audit({"kind": "pid_gone", **plan})
            return plan
        # Also re-verify the image name hasn't changed (PID reuse defence)
        if (recheck.get("name") or "").lower() != (plan["image_name"] or "").lower():
            plan["status"]       = "pid_reused_different_image"
            plan["recheck_name"] = recheck.get("name")
            _audit({"kind": "pid_reuse", **plan})
            return plan

        ok, msg = _run_action(plan["action"], plan.get("priority_level"), plan["pid"])
        plan["status"] = "executed" if ok else "execute_failed"
        plan["result"] = msg
        _audit({"kind": "executed" if ok else "failed", **plan})

        after = process_state(plan["pid"])
        plan["process_state_after"] = after
        try:
            await nc.publish(
                AUDIT_SUBJECT,
                json.dumps({**plan, "kind": "post_exec"}).encode()
            )
        except Exception:
            pass
        return plan
    finally:
        await nc.drain()


# ─────────────────────────────────────────────────────────────────────
# SNAPSHOT + DAEMON (windows.process.metrics feed, mirrors Cap#1 cadence)
# ─────────────────────────────────────────────────────────────────────

def _snapshot(top_n: int = 25) -> dict:
    procs = list_processes()
    procs_sorted = sorted(procs, key=lambda p: p["working_set_mb"], reverse=True)
    top = procs_sorted[:top_n]
    total_mem_mb = round(sum(p["working_set_mb"] for p in procs), 1)
    return {
        "timestamp":    _utc(),
        "host":         os.environ.get("COMPUTERNAME") or "",
        "proc_count":   len(procs),
        "total_mem_mb": total_mem_mb,
        "top_by_mem":   top,
    }


async def _publish_snapshot(nc=None) -> dict:
    snap = _snapshot()
    try:
        if nc is None:
            import nats as _nats
            nc_local = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                await nc_local.publish(METRICS_SUBJECT, json.dumps(snap).encode())
            finally:
                await nc_local.drain()
        else:
            await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
    except Exception as e:
        print(f"[PROC-MGR] snapshot publish failed: {e!r}")
    return snap


async def _classify_top(n: int = 15) -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=3)
    try:
        procs = list_processes()
        procs.sort(key=lambda p: p["working_set_mb"], reverse=True)
        selected = procs[:n]
        print(f"[PROC-MGR] classifying top {len(selected)} by memory via tenet5.liril.classify…")
        await _publish_snapshot(nc)
        for p in selected:
            cls = await _classify_via_npu(nc, p)
            payload = {
                "kind":      "classification",
                "timestamp": _utc(),
                "process":   p,
                **cls,
                "denied":    _is_denied(p["pid"], p["name"]),
            }
            try:
                await nc.publish(AUDIT_SUBJECT, json.dumps(payload).encode())
            except Exception:
                pass
            _audit(payload)
            risk = cls.get("process_classification", "?")
            print(f"  pid={p['pid']:>6}  {p['name'][:32]:32s}  "
                  f"mem={p['working_set_mb']:>7.1f}MB  risk={risk}  "
                  f"denied={_is_denied(p['pid'], p['name'])}")
    finally:
        await nc.drain()


async def _daemon(interval_min: int = 5, top_n: int = 15) -> None:
    print(f"[PROC-MGR] daemon started — classify-top {top_n} every {interval_min} min, "
          f"snapshot every cycle")
    while True:
        try:
            await _classify_top(top_n)
        except Exception as e:
            print(f"[PROC-MGR] cycle error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_min * 60)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Windows Process Management — Capability #3")
    ap.add_argument("--list",            action="store_true", help="List all processes")
    ap.add_argument("--classify",        type=str, metavar="PID_OR_NAME",
                    help="Classify ONE process (integer PID or image name) via NPU")
    ap.add_argument("--classify-top",    type=int, metavar="N",
                    help="Classify top-N processes by memory")
    ap.add_argument("--classify-all",    action="store_true",
                    help="Classify all processes — slow")
    ap.add_argument("--plan",            nargs=2, metavar=("ACTION", "PID"),
                    help="Build a plan and publish — DRY RUN. "
                         "ACTION ∈ {terminate, suspend, resume, priority:LEVEL}")
    ap.add_argument("--execute",         nargs=2, metavar=("ACTION", "PID"),
                    help="Execute the plan. Requires LIRIL_EXECUTE=1.")
    ap.add_argument("--reason",          type=str, default="",
                    help="Reason string attached to the plan")
    ap.add_argument("--daemon",          action="store_true",
                    help="Run classify-top daemon (5-min loop)")
    ap.add_argument("--daemon-interval", type=int, default=5,
                    help="Daemon interval in minutes (default 5)")
    ap.add_argument("--daemon-top",      type=int, default=15,
                    help="Daemon top-N classify count (default 15)")
    ap.add_argument("--snapshot",        action="store_true",
                    help="Publish one windows.process.metrics snapshot and exit")
    ap.add_argument("--show-denylist",   action="store_true",
                    help="Print the hard-coded denylist and exit")
    ap.add_argument("--show-allowlist",  action="store_true",
                    help="Print the user-curated allowlist and exit")
    args = ap.parse_args()

    if args.show_denylist:
        for d in sorted(_DENYLIST_NAMES):
            print(d)
        return 0
    if args.show_allowlist:
        al = _load_allowlist()
        if not al:
            print(f"# allowlist is empty — add process names to {ALLOWLIST_FILE}")
        else:
            for a in sorted(al):
                print(a)
        return 0

    if args.list:
        procs = list_processes()
        procs.sort(key=lambda p: p["working_set_mb"], reverse=True)
        for p in procs:
            denied = "DENY" if _is_denied(p["pid"], p["name"]) else "    "
            print(f"  {denied} pid={p['pid']:>6}  {p['name'][:32]:32s}  "
                  f"mem={p['working_set_mb']:>8.1f}MB  threads={p['threads']:>4}")
        return 0

    if args.classify:
        async def run():
            import nats as _nats
            nc = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                target = args.classify.strip()
                if target.isdigit():
                    st = process_state(int(target))
                    if not st:
                        print("process not found")
                        return
                    cls = await _classify_via_npu(nc, st)
                    print(json.dumps({"process": st, **cls,
                                      "denied": _is_denied(st["pid"], st["name"])}, indent=2))
                else:
                    pids = resolve_pids_by_name(target)
                    if not pids:
                        print(f"no live PIDs for name {target!r}")
                        return
                    for pid in pids:
                        st = process_state(pid)
                        if not st:
                            continue
                        cls = await _classify_via_npu(nc, st)
                        print(json.dumps({"process": st, **cls,
                                          "denied": _is_denied(st["pid"], st["name"])}, indent=2))
            finally:
                await nc.drain()
        asyncio.run(run())
        return 0

    if args.classify_top:
        asyncio.run(_classify_top(max(1, int(args.classify_top))))
        return 0

    if args.classify_all:
        async def run():
            procs = list_processes()
            await _classify_top(len(procs))
        asyncio.run(run())
        return 0

    if args.plan:
        action, pid_s = args.plan
        try:
            pid = int(pid_s)
        except ValueError:
            print(f"invalid PID {pid_s!r} — mutations require an integer PID")
            return 2
        os.environ["LIRIL_EXECUTE"] = "0"
        plan = asyncio.run(do_action(action, pid, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0

    if args.execute:
        action, pid_s = args.execute
        try:
            pid = int(pid_s)
        except ValueError:
            print(f"invalid PID {pid_s!r} — mutations require an integer PID")
            return 2
        if not EXEC_GATE:
            print("EXEC_GATE off — set LIRIL_EXECUTE=1 to execute. Refusing.")
            return 2
        plan = asyncio.run(do_action(action, pid, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0 if plan.get("status") == "executed" else 1

    if args.snapshot:
        snap = asyncio.run(_publish_snapshot())
        print(json.dumps(snap, indent=2))
        return 0

    if args.daemon:
        asyncio.run(_daemon(int(args.daemon_interval), int(args.daemon_top)))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
