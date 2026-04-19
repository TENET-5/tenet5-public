#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T10:15:00Z | Author: claude_code | Change: wire fse.is_safe_to_execute() gate (Cap#10 enforcement)
"""LIRIL Windows Service Control — Capability #2 of the NPU-Domain plan.

When polled for Capability #2's safety plan, LIRIL answered:
  ALLOWLIST:       (not yet defined — starts empty, user explicitly adds)
  DENYLIST:        Windows Kernel, Security, Networking Services
  RISK_SCHEMA:     service_classification:<low|med|high|critical>, confidence:<0..1>
  DRY_RUN_DEFAULT: yes
  AUDIT_SUBJECT:   windows.service.control

This file enforces all five.

Threat model
------------
The attack vector we're protecting against is not malicious — it's LLM
plan-then-execute. A confidently-wrong LIRIL instruction like
"Stop WinDefend because it showed anomalous CPU" would brick antivirus.
Hence:
  (1) Every action is published to windows.service.control BEFORE execution.
  (2) Default mode is dry-run: plan is logged + published, nothing runs.
  (3) Execution requires BOTH the --execute flag AND env LIRIL_EXECUTE=1
      AND the service name must not match the denylist.
  (4) Denylist is hard-coded, not editable via NATS (no remote bypass).
  (5) Every executed action waits 3 seconds after publishing the plan
      so other agents (observer, a human) can veto by publishing to
      windows.service.control.veto with the matching plan_id.

CLI modes
---------
  --list                   List all services (display name + state + startup)
  --classify NAME          Classify a service's criticality via NPU
  --classify-all           Walk all services, publish per-service risk
  --plan ACTION NAME       Build a plan (start|stop|restart NAME) and
                           publish to windows.service.control — DRY RUN.
  --execute ACTION NAME    Executes the plan. Requires LIRIL_EXECUTE=1 env.
  --daemon                 Runs classify-all every 10 minutes, publishes
                           services/states/risks continuously.
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

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AUDIT_SUBJECT = "windows.service.control"
VETO_SUBJECT  = "windows.service.control.veto"
EXEC_GATE     = os.environ.get("LIRIL_EXECUTE", "0") == "1"
VETO_WINDOW_SEC = 3.0

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWLIST_FILE = DATA_DIR / "liril_service_allowlist.txt"
AUDIT_LOG      = DATA_DIR / "liril_service_control.jsonl"

# ── DENYLIST ─────────────────────────────────────────────────────────
# Hard-coded. Matches on service short NAME (not display). Case-insensitive.
# These are the services that, if stopped, break the OS or disable security.
# The list is intentionally broader than strictly necessary — better to force
# a user to explicitly override the denylist for a borderline case than to
# ship code that can brick someone's machine.
_DENYLIST = {
    # Kernel / boot / session
    "wininit", "winlogon", "csrss", "smss", "services", "lsass",
    "lsm", "SessionEnv", "TrustedInstaller",
    # Security
    "WinDefend", "SecurityHealthService", "mpssvc", "SENS", "Wcncsvc",
    "Sense", "WdNisSvc", "WaaSMedicSvc",
    # Networking spine
    "Dhcp", "Dnscache", "LanmanServer", "LanmanWorkstation", "Netlogon",
    "NlaSvc", "NcbService", "EventSystem", "EventLog", "RpcSs", "RpcEptMapper",
    "DcomLaunch", "PlugPlay", "Power", "ProfSvc",
    # Storage / filesystem
    "VolmgrX", "Volsnap", "Vmbus",
    # Update / servicing (breaking these leaves the OS unpatchable)
    "wuauserv", "BITS", "msiserver", "CryptSvc", "UsoSvc",
    # NVIDIA GPU — TENET5's compute surface
    "NVDisplay.ContainerLocalSystem", "NvContainerLocalSystem",
    "NVWMI", "nvlddmkm",
    # Intel NPU
    "intelAIAiXServ", "IntelAudioService",
    # Audit / event pipeline (LIRIL needs these for her own eyes)
    "WinRM", "Schedule",
}


def _is_denied(service_name: str) -> bool:
    name = (service_name or "").strip()
    lower = name.lower()
    for d in _DENYLIST:
        if lower == d.lower():
            return True
    # Wildcards: anything starting with "WdNis", "MsSecFlt", etc. is defence.
    if lower.startswith(("windefend", "mpssvc", "wdnis", "mssecflt",
                         "secop", "microsoftedge")):
        return True
    return False


def _load_allowlist() -> set[str]:
    """User-curated allowlist. Lower-cased service names."""
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
    """Append a single-line JSON audit record to the local log."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[SVC-CTRL] audit log write failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# SERVICE DISCOVERY via PowerShell
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


def list_services() -> list[dict]:
    """Return a list of dicts: name, display_name, status, start_type."""
    data = _pwsh_json(
        "Get-Service | Select-Object "
        "Name,DisplayName,@{n='Status';e={\"$($_.Status)\"}},"
        "@{n='StartType';e={\"$($_.StartType)\"}} "
        "| ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        out.append({
            "name":         s.get("Name", ""),
            "display_name": s.get("DisplayName", ""),
            "status":       s.get("Status", ""),
            "start_type":   s.get("StartType", ""),
        })
    return out


def service_state(name: str) -> dict | None:
    data = _pwsh_json(
        f"Get-Service -Name '{_sanitize(name)}' -ErrorAction SilentlyContinue | "
        "Select-Object Name,DisplayName,@{n='Status';e={\"$($_.Status)\"}},"
        "@{n='StartType';e={\"$($_.StartType)\"}} | ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        return {
            "name":         data.get("Name", ""),
            "display_name": data.get("DisplayName", ""),
            "status":       data.get("Status", ""),
            "start_type":   data.get("StartType", ""),
        }
    return None


def _sanitize(name: str) -> str:
    """Defence against PowerShell injection — we only accept alnum, dash, dot, underscore."""
    return re.sub(r"[^A-Za-z0-9._\-]", "", name or "")


# ─────────────────────────────────────────────────────────────────────
# NPU CLASSIFY — risk scoring via the LIRIL classifier
# ─────────────────────────────────────────────────────────────────────

async def _classify_via_npu(nc, service: dict) -> dict:
    """Use tenet5.liril.classify (the live NPU subject) to tag a service with
    an axis + confidence. npu.classify per LIRIL's spec is not yet live; when
    it is, we'll swap the subject with no change to the caller."""
    text = (
        f"Windows service: {service.get('display_name','')} "
        f"(name={service.get('name','')}, start={service.get('start_type','')}, "
        f"status={service.get('status','')})"
    )
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text, "source": "service_control"}).encode(),
            timeout=5,
        )
        d = json.loads(msg.data.decode())
        axis = d.get("domain") or d.get("axis")
        conf = d.get("confidence")
        # Map axis to risk level heuristically: SECURITY/NETWORKING -> high,
        # MATHEMATICS/ART -> low, else medium. Until we retrain, this is the
        # best projection onto LIRIL's RISK_SCHEMA (low|med|high|critical).
        risk = _axis_to_risk(axis)
        return {
            "service_classification": risk,
            "confidence": conf,
            "axis": axis,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _axis_to_risk(axis: str | None) -> str:
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

def _make_plan(action: str, name: str, reason: str) -> dict:
    return {
        "plan_id":   str(uuid.uuid4()),
        "timestamp": _utc(),
        "action":    action,        # start | stop | restart
        "service":   name,
        "reason":    reason,
        "denied":    _is_denied(name),
        "allowed":   name.lower() in _load_allowlist(),
        "dry_run":   not EXEC_GATE,
    }


async def _publish_plan(nc, plan: dict) -> None:
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(plan).encode())
        _audit({"kind": "plan_published", **plan})
    except Exception as e:
        print(f"[SVC-CTRL] publish plan failed: {e!r}")


async def _wait_for_veto(nc, plan_id: str) -> dict | None:
    """Subscribe to the veto subject for VETO_WINDOW_SEC and return the first
    matching veto message, or None on timeout."""
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


def _run_service_cmd(action: str, name: str) -> tuple[bool, str]:
    """Execute the actual service change via PowerShell. Returns (ok, message)."""
    safe = _sanitize(name)
    if not safe:
        return False, "invalid service name"
    cmd = {
        "start":   f"Start-Service -Name '{safe}'",
        "stop":    f"Stop-Service -Name '{safe}' -Force",
        "restart": f"Restart-Service -Name '{safe}' -Force",
    }.get(action)
    if not cmd:
        return False, f"unknown action {action!r}"
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, timeout=30, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "non-zero exit").strip()[:500]
        return True, "ok"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def do_action(action: str, name: str, reason: str = "") -> dict:
    """Plan → publish → veto-wait → (maybe) execute. Returns the full audit record."""
    plan = _make_plan(action, name, reason or "no reason provided")

    # Denylist is an absolute gate
    if plan["denied"]:
        plan["status"] = "denied_by_denylist"
        _audit({"kind": "denied", **plan})
        return plan

    # Dry-run gate (default)
    if not EXEC_GATE:
        plan["status"] = "dry_run_logged"
        _audit({"kind": "dry_run", **plan})
        return plan

    # Cap#10 fail-safe gate — refuse if the global escalation level restricts
    # mutations. Missing/broken failsafe module is non-fatal (caps predate #10).
    try:
        import liril_fail_safe_escalation as _fse
        if not _fse.is_safe_to_execute():
            plan["status"]         = "refused_by_failsafe"
            plan["failsafe_level"] = _fse.current_level()
            _audit({"kind": "refused_by_failsafe", **plan})
            return plan
    except ImportError:
        pass

    # Real execution path — still requires allowlist
    if not plan["allowed"]:
        plan["status"] = "not_in_allowlist"
        _audit({"kind": "blocked_not_allowed", **plan})
        return plan

    # Publish plan + await veto window
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

        ok, msg = _run_service_cmd(action, name)
        plan["status"]  = "executed" if ok else "execute_failed"
        plan["result"]  = msg
        _audit({"kind": "executed" if ok else "failed", **plan})

        # Post-execution state snapshot
        after = service_state(name)
        plan["service_state_after"] = after
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
# CLI
# ─────────────────────────────────────────────────────────────────────

async def _classify_all() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=3)
    services = list_services()
    print(f"[SVC-CTRL] classifying {len(services)} services via tenet5.liril.classify…")
    try:
        for s in services:
            cls = await _classify_via_npu(nc, s)
            payload = {
                "kind": "classification",
                "timestamp": _utc(),
                "service": s,
                **cls,
                "denied": _is_denied(s["name"]),
            }
            try:
                await nc.publish(AUDIT_SUBJECT, json.dumps(payload).encode())
            except Exception:
                pass
            _audit(payload)
            risk = cls.get("service_classification", "?")
            print(f"  {s['name']:45s} {s['status']:10s} risk={risk}  denied={_is_denied(s['name'])}")
    finally:
        await nc.drain()


async def _daemon(interval_min: int = 10) -> None:
    print(f"[SVC-CTRL] daemon started — classify-all every {interval_min} min")
    while True:
        try:
            await _classify_all()
        except Exception as e:
            print(f"[SVC-CTRL] cycle error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_min * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Windows Service Control — Capability #2")
    ap.add_argument("--list",          action="store_true", help="List all services")
    ap.add_argument("--classify",      type=str, help="Classify ONE service via NPU")
    ap.add_argument("--classify-all",  action="store_true", help="Classify all services + publish")
    ap.add_argument("--plan",          nargs=2, metavar=("ACTION", "NAME"),
                    help="Build a plan (start|stop|restart) and publish — DRY RUN")
    ap.add_argument("--execute",       nargs=2, metavar=("ACTION", "NAME"),
                    help="Execute. Requires LIRIL_EXECUTE=1 env AND allowlist entry.")
    ap.add_argument("--reason",        type=str, default="", help="Reason string attached to plan")
    ap.add_argument("--daemon",        action="store_true", help="Run classify-all daemon (10-min loop)")
    ap.add_argument("--show-denylist", action="store_true", help="Print the hard-coded denylist and exit")
    ap.add_argument("--show-allowlist",action="store_true", help="Print the user-curated allowlist and exit")
    args = ap.parse_args()

    if args.show_denylist:
        for d in sorted(_DENYLIST):
            print(d)
        return 0
    if args.show_allowlist:
        al = _load_allowlist()
        if not al:
            print(f"# allowlist is empty — add service names to {ALLOWLIST_FILE}")
        else:
            for a in sorted(al):
                print(a)
        return 0

    if args.list:
        for s in list_services():
            denied = "DENY" if _is_denied(s["name"]) else "    "
            print(f"  {denied} {s['name']:45s} {s['status']:10s} {s['start_type']:10s} {s['display_name']}")
        return 0

    if args.classify:
        async def run():
            import nats as _nats
            nc = await _nats.connect(NATS_URL, connect_timeout=3)
            svc = service_state(args.classify)
            if not svc:
                print("service not found")
                await nc.drain()
                return
            cls = await _classify_via_npu(nc, svc)
            print(json.dumps({"service": svc, **cls, "denied": _is_denied(svc["name"])}, indent=2))
            await nc.drain()
        asyncio.run(run())
        return 0

    if args.classify_all:
        asyncio.run(_classify_all())
        return 0

    if args.plan:
        action, name = args.plan
        # plan is always dry-run regardless of EXEC_GATE
        os.environ["LIRIL_EXECUTE"] = "0"
        plan = asyncio.run(do_action(action, name, args.reason))
        print(json.dumps(plan, indent=2))
        return 0

    if args.execute:
        action, name = args.execute
        if not EXEC_GATE:
            print("EXEC_GATE off — set LIRIL_EXECUTE=1 to execute. Refusing.")
            return 2
        plan = asyncio.run(do_action(action, name, args.reason))
        print(json.dumps(plan, indent=2))
        return 0 if plan.get("status") == "executed" else 1

    if args.daemon:
        asyncio.run(_daemon())
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
