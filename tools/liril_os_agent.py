#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T00:05:00Z | Author: claude_code | Change: LIRIL OS agent — Windows + tenet5-linux
"""LIRIL OS Agent — dual-domain system introspection and improvement.

User objective (2026-04-19):
  "I want LIRIL to get full domain over the Windows systems with the NPU —
   and run security scans as well as learn how Windows functions and
   test and create improvements to our local OS … as well as our DOCKERED
   linux run time with tenet5 — we're ultimately upgrading Windows to tenet5"

This agent gives LIRIL visibility into BOTH:
  (a) The host Windows OS — processes, services, Windows Defender threat
      history, critical event log entries, scheduled tasks, open ports,
      pending updates.
  (b) The tenet5-linux Docker runtime — container health, guest OS API,
      NATS guest bridge, subkernel state.

It also runs a *capability gap* scan: which Windows functions does
tenet5-linux not yet replicate? Each gap becomes a dev-team backlog
task (WS-OS-*) so the autonomous daemon can work toward parity over
time — the long arc toward replacing Windows with tenet5.

Subjects served:
  tenet5.liril.os.scan_windows  — request-reply: full Windows snapshot
  tenet5.liril.os.scan_tenet5   — request-reply: full tenet5-linux snapshot
  tenet5.liril.os.gap           — request-reply: Windows→tenet5 capability gaps
  tenet5.liril.os.findings      — pub-only: each scan emits findings here

CLI modes:
  python tools/liril_os_agent.py --scan-windows
  python tools/liril_os_agent.py --scan-tenet5
  python tools/liril_os_agent.py --gap
  python tools/liril_os_agent.py --file-backlog  (turn gaps into WS-OS-* tasks)
  python tools/liril_os_agent.py --daemon        (runs all + publishes, every 20 min)

Run via pythonw to avoid console popups. Whitelisted in liril_popup_hunter.
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
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
BACKLOG = Path(r"E:/TENET-5.github.io/data/liril_work_schedule.json")
REPORT_DIR = Path(r"E:/TENET-5.github.io/data/liril_os_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Subprocess.run wrapper that hides console windows.

    Explicit utf-8 decoding with errors='replace' so Windows CP-1252
    bytes in PowerShell output don't raise UnicodeDecodeError.
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
        err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
        return r.returncode, out, err
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        return -2, "", f"{type(e).__name__}: {e}"


def _pwsh(script: str, timeout: int = 20) -> dict:
    """Run a PowerShell command and return parsed JSON output."""
    rc, out, err = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )
    if rc != 0:
        return {"error": err.strip() or f"rc={rc}"}
    out = out.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Return as raw lines if not JSON
        return {"raw": out.splitlines()}


# ────────────────────────────────────────────────────────────────
# WINDOWS SCAN
# ────────────────────────────────────────────────────────────────

def scan_windows() -> dict:
    """Snapshot the host Windows state: processes, services, Defender
    threat history, critical event log, scheduled tasks, open listeners.
    """
    out: dict = {"timestamp": _utc(), "domain": "windows", "findings": []}

    # Top 10 processes by memory (names only — no PII)
    procs = _pwsh(
        "Get-Process | Sort-Object WorkingSet -Desc | Select-Object -First 10 "
        "ProcessName,Id,@{n='wss_mb';e={[int]($_.WorkingSet/1MB)}} | ConvertTo-Json -Compress"
    )
    out["top_processes_by_memory"] = procs if isinstance(procs, list) else (
        [procs] if procs and "error" not in procs else []
    )

    # Running services count + non-running critical services
    svcs = _pwsh(
        "Get-Service | Group-Object Status | Select-Object Name,Count | ConvertTo-Json -Compress"
    )
    out["services_by_status"] = svcs if isinstance(svcs, list) else [svcs] if svcs else []

    # Windows Defender threat history (last 30 days, top 5)
    defender = _pwsh(
        "try { Get-MpThreatDetection -ErrorAction SilentlyContinue | "
        "Sort-Object InitialDetectionTime -Desc | Select-Object -First 5 "
        "ThreatName,Resources,InitialDetectionTime | ConvertTo-Json -Compress } "
        "catch { @{error='MpThreatDetection unavailable'} | ConvertTo-Json -Compress }"
    )
    out["defender_recent_threats"] = defender if defender else []
    if isinstance(defender, list) and defender:
        out["findings"].append({
            "severity": "high",
            "category": "defender",
            "summary": f"{len(defender)} Defender threat detections in recent history",
            "detail": defender[:3],
        })

    # Defender real-time protection status
    defender_status = _pwsh(
        "try { Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled,"
        "IoavProtectionEnabled,AMServiceEnabled,AntispywareEnabled,NISEnabled | "
        "ConvertTo-Json -Compress } catch { @{error='MpComputerStatus unavailable'} | ConvertTo-Json -Compress }"
    )
    out["defender_status"] = defender_status
    if isinstance(defender_status, dict) and defender_status.get("AntivirusEnabled") is False:
        out["findings"].append({
            "severity": "critical",
            "category": "defender",
            "summary": "Windows Defender antivirus is disabled",
            "detail": defender_status,
        })

    # Event log: System channel, Critical/Error, last 24h, top 5
    events = _pwsh(
        "try { Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;"
        "StartTime=(Get-Date).AddDays(-1)} -ErrorAction SilentlyContinue -MaxEvents 5 | "
        "Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,@{n='msg';e={$_.Message.Substring(0,[Math]::Min(200,$_.Message.Length))}} | "
        "ConvertTo-Json -Compress } catch { @() | ConvertTo-Json -Compress }",
        timeout=25,
    )
    out["recent_critical_system_events"] = events if events else []
    if isinstance(events, list) and events:
        out["findings"].append({
            "severity": "medium",
            "category": "event_log",
            "summary": f"{len(events)} critical/error System events in last 24h",
            "detail": events[:3],
        })

    # Scheduled tasks — Ready state count (ignoring Microsoft's)
    tasks = _pwsh(
        "try { (Get-ScheduledTask | Where-Object { $_.State -eq 'Ready' -and "
        "$_.TaskPath -notlike '\\Microsoft\\*' }).Count } catch { 0 }"
    )
    out["ready_non_ms_scheduled_tasks"] = tasks

    # Open TCP listeners (ports only — no binding metadata)
    ports = _pwsh(
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object LocalPort -Unique | Sort-Object LocalPort | ConvertTo-Json -Compress"
    )
    out["listen_ports"] = [p.get("LocalPort") for p in ports] if isinstance(ports, list) else []

    # Windows Update pending reboot
    reboot_pending = _pwsh(
        "[bool](Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
        "Component Based Servicing\\RebootPending' -ErrorAction SilentlyContinue)"
    )
    out["reboot_pending"] = reboot_pending if isinstance(reboot_pending, dict) else {
        "raw_value": reboot_pending
    }

    return out


# ────────────────────────────────────────────────────────────────
# TENET5-LINUX (Docker runtime) SCAN
# ────────────────────────────────────────────────────────────────

def scan_tenet5_linux() -> dict:
    out: dict = {"timestamp": _utc(), "domain": "tenet5-linux", "findings": []}

    # Container list — match tenet5 AND k8s_*tenet5* AND nemoclaw bridge
    rc, stdout, stderr = _run(
        ["docker", "ps", "--format", "{{json .}}"],
        timeout=10,
    )
    all_containers = []
    if rc == 0:
        for line in stdout.splitlines():
            try:
                all_containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Keep only TENET5-family containers
    containers = [
        c for c in all_containers
        if "tenet5" in (c.get("Names") or "").lower()
        or "nemo" in (c.get("Names") or "").lower()
        or "liril" in (c.get("Names") or "").lower()
    ]
    out["containers"] = containers

    unhealthy = [c for c in containers
                 if "unhealthy" in (c.get("Status") or "").lower()
                 or "exited" in (c.get("Status") or "").lower()
                 or "restart" in (c.get("Status") or "").lower()]
    if unhealthy:
        out["findings"].append({
            "severity": "high",
            "category": "container_health",
            "summary": f"{len(unhealthy)} tenet5 container(s) not healthy",
            "detail": [c.get("Names") + " — " + c.get("Status") for c in unhealthy],
        })

    # Guest OS API health (port 18090 from host)
    rc, stdout, _ = _run(
        ["curl", "-s", "-m", "3", "http://127.0.0.1:18090/health"],
        timeout=6,
    )
    out["guest_os_api"] = {
        "up": rc == 0 and ("ok" in stdout.lower() or "health" in stdout.lower()),
        "body": stdout[:200],
    }
    if not out["guest_os_api"]["up"]:
        out["findings"].append({
            "severity": "medium",
            "category": "guest_api",
            "summary": "tenet5-os /health endpoint unreachable at :18090",
        })

    # NATS guest bridge (14222 from host)
    rc, stdout, _ = _run(
        ["curl", "-s", "-m", "3", "http://127.0.0.1:18222/varz"],
        timeout=6,
    )
    out["guest_nats_bridge"] = {
        "up": rc == 0 and stdout.strip().startswith("{"),
        "varz_bytes": len(stdout),
    }

    # Subkernel status via NATS
    sk = asyncio.run(_nats_request("tenet5.liril.subkernel.status", {}, timeout=5))
    out["subkernel"] = sk

    return out


async def _nats_request(subject: str, payload: dict, timeout: int = 5) -> dict:
    try:
        import nats as _nats
    except ImportError:
        return {"error": "nats-py missing"}
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        return {"error": f"nats-connect: {e!r}"}
    try:
        msg = await nc.request(subject, json.dumps(payload).encode(), timeout=timeout)
        try:
            return json.loads(msg.data.decode())
        except Exception:
            return {"raw": msg.data.decode()[:500]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await nc.drain()


# ────────────────────────────────────────────────────────────────
# CAPABILITY GAP: Windows features tenet5-linux doesn't replicate yet
# ────────────────────────────────────────────────────────────────

# Each tuple: (area, windows_feature, tenet5_equivalent_path, unlocks_if_built)
WINDOWS_CAPABILITIES = [
    ("boot", "secure boot + BitLocker volume encryption",
     "tenet5-linux LUKS + signed kernel", "trusted-boot parity"),
    ("process", "Task Manager + Resource Monitor UI",
     "tenet5 dashboard :8091 lists GPUs but not processes", "live process management"),
    ("security", "Windows Defender real-time protection + threat history",
     "tenet5 has no resident AV — only URL gate + hallucination filter",
     "host-level malware detection"),
    ("firewall", "Windows Defender Firewall with app-based rules",
     "tenet5-linux relies on container iptables only", "app-level firewall"),
    ("update", "Windows Update push + rollback",
     "tenet5 updates by git pull + service restart", "atomic rollback-safe update"),
    ("audio", "Windows audio stack (WASAPI)",
     "tenet5-linux has no audio path", "voice I/O for LIRIL"),
    ("display", "DWM compositor + GPU-accelerated window manager",
     "tenet5 has weston via VNC :15900", "native (non-VNC) display"),
    ("printing", "Windows Print Spooler",
     "no equivalent in tenet5", "file→print"),
    ("input", "low-level keyboard/mouse/touch APIs",
     "tenet5-linux has evdev via weston but no direct capture", "automation + accessibility"),
    ("filesys", "NTFS with per-file ACLs + VSS snapshots",
     "tenet5-linux uses ext4 / overlay without snapshots", "file-history recovery"),
    ("scheduler", "Task Scheduler with multiple trigger types",
     "tenet5 has cron + NATS pub but no UI", "scheduled-task UI"),
    ("registry", "centralized hierarchical config (HKLM/HKCU)",
     "tenet5 spreads config across JSON + env", "unified config store"),
    ("pnp", "Plug-and-Play device enumeration",
     "tenet5-linux has libudev but no LIRIL binding", "hot-plug awareness"),
    ("accessibility", "Narrator + UIA tree",
     "tenet5 has Clara voice narration but no UIA", "full assistive API"),
    ("network", "Windows Network Location Awareness service",
     "tenet5 has NATS but no captive-portal / network-change awareness",
     "roaming-aware agents"),
]


def capability_gap() -> dict:
    """Return the current Windows→tenet5 capability gap as a structured list.
    Each entry can become a dev-team backlog task.
    """
    entries = []
    for area, feature, path, unlocks in WINDOWS_CAPABILITIES:
        entries.append({
            "area":              area,
            "windows_feature":   feature,
            "tenet5_status":     path,
            "unlocks_if_built":  unlocks,
            "proposed_task_id":  f"WS-OS-{area}",
        })
    return {
        "timestamp":  _utc(),
        "source":     "liril_os_agent.capability_gap",
        "total_gaps": len(entries),
        "entries":    entries,
        "note": (
            "Each entry is a candidate dev-team backlog task. Passing --file-backlog "
            "will create WS-OS-* tasks so the autonomous daemon can iterate toward "
            "Windows→tenet5 upgrade parity one capability at a time."
        ),
    }


def file_gap_as_backlog() -> int:
    """Turn capability_gap() entries into pending WS-OS-* backlog tasks.
    Only adds entries that don't already have a task with that id.
    """
    if not BACKLOG.exists():
        print(f"backlog not found: {BACKLOG}")
        return 0
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    existing_ids = {t.get("id") for t in data.get("backlog", [])}
    added = 0
    for entry in capability_gap()["entries"]:
        tid = entry["proposed_task_id"]
        if tid in existing_ids:
            continue
        data["backlog"].append({
            "id":            tid,
            "title":         f"OS upgrade — {entry['windows_feature']} \u2192 tenet5 parity",
            "axis_domain":   "TECHNOLOGY",
            "priority":      "low",
            "role_pipeline": ["researcher", "architect", "engineer", "editor", "gatekeeper"],
            "target_files": [],
            "acceptance": (
                f"Close the Windows\u2192tenet5 capability gap for: "
                f"{entry['windows_feature']}. Current tenet5 status: {entry['tenet5_status']}. "
                f"Acceptance: write a design note at docs/os_gaps/{entry['area']}.md "
                f"(or extend existing doc) describing (1) what Windows does, (2) what "
                f"tenet5-linux can do today, (3) proposed concrete next step toward parity."
            ),
            "context": (
                f"Area: {entry['area']}. Unlocks: {entry['unlocks_if_built']}. "
                f"Filed by liril_os_agent.file_gap_as_backlog()."
            ),
            "status":        "pending",
            "created_at":    _utc(),
        })
        added += 1
    if added:
        data["updated_at"] = _utc()
        BACKLOG.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return added


# ────────────────────────────────────────────────────────────────
# NPU CLASSIFICATION — severity tagging via LIRIL NPU
# ────────────────────────────────────────────────────────────────

async def _npu_classify(text: str) -> dict:
    try:
        import nats as _nats
    except ImportError:
        return {"error": "nats-py missing"}
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        return {"error": f"nats-connect: {e!r}"}
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text, "source": "os_agent"}).encode(),
            timeout=5,
        )
        return json.loads(msg.data.decode())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await nc.drain()


async def classify_findings(findings: list[dict]) -> list[dict]:
    """Tag each finding with LIRIL NPU classification (axis domain + confidence)."""
    for f in findings:
        summary = f.get("summary", "")
        if not summary:
            continue
        cls = await _npu_classify(summary)
        if "error" not in cls:
            f["npu_classification"] = {
                "axis":        cls.get("domain") or cls.get("axis"),
                "confidence":  cls.get("confidence"),
            }
    return findings


# ────────────────────────────────────────────────────────────────
# PUBLISH FINDINGS
# ────────────────────────────────────────────────────────────────

async def publish_findings(report: dict) -> str:
    try:
        import nats as _nats
    except ImportError:
        return "nats-py missing"
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        return f"connect: {e!r}"
    try:
        await nc.publish(
            "tenet5.liril.os.findings",
            json.dumps(report, default=str).encode(),
        )
        await nc.flush(timeout=3)
    finally:
        await nc.drain()
    return "published"


# ────────────────────────────────────────────────────────────────
# NATS SUBJECT HANDLERS (daemon mode)
# ────────────────────────────────────────────────────────────────

async def daemon(interval_min: int = 20) -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[OS-AGENT] subscribed to tenet5.liril.os.* on {NATS_URL}")

    async def _h_scan_windows(msg):
        r = scan_windows()
        r["findings"] = await classify_findings(r["findings"])
        await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def _h_scan_tenet5(msg):
        r = scan_tenet5_linux()
        r["findings"] = await classify_findings(r["findings"])
        await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def _h_gap(msg):
        await nc.publish(msg.reply, json.dumps(capability_gap(), default=str).encode())

    await nc.subscribe("tenet5.liril.os.scan_windows", cb=_h_scan_windows)
    await nc.subscribe("tenet5.liril.os.scan_tenet5",  cb=_h_scan_tenet5)
    await nc.subscribe("tenet5.liril.os.gap",          cb=_h_gap)

    # Periodic proactive scans
    while True:
        try:
            report = {
                "timestamp": _utc(),
                "windows":   scan_windows(),
                "tenet5":    scan_tenet5_linux(),
                "gap":       capability_gap(),
            }
            report["windows"]["findings"] = await classify_findings(report["windows"]["findings"])
            report["tenet5"]["findings"]  = await classify_findings(report["tenet5"]["findings"])
            (REPORT_DIR / f"scan_{int(time.time())}.json").write_text(
                json.dumps(report, indent=2, default=str),
                encoding="utf-8",
            )
            await nc.publish(
                "tenet5.liril.os.findings",
                json.dumps(report, default=str).encode(),
            )
        except Exception as e:
            print(f"[OS-AGENT] periodic scan error: {e!r}")
        await asyncio.sleep(interval_min * 60)


# ────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL OS agent — Windows + tenet5-linux")
    ap.add_argument("--scan-windows", action="store_true")
    ap.add_argument("--scan-tenet5",  action="store_true")
    ap.add_argument("--gap",          action="store_true")
    ap.add_argument("--file-backlog", action="store_true",
                    help="turn capability gaps into pending WS-OS-* backlog tasks")
    ap.add_argument("--daemon",       action="store_true",
                    help="run periodic scans + serve tenet5.liril.os.* subjects")
    ap.add_argument("--interval",     type=int, default=20,
                    help="daemon-mode scan interval in minutes")
    ap.add_argument("--classify",     action="store_true",
                    help="NPU-classify findings (costs one npu.classify call per finding)")
    ap.add_argument("--publish",      action="store_true",
                    help="publish report to tenet5.liril.os.findings")
    ap.add_argument("--json",         action="store_true", help="emit JSON")
    args = ap.parse_args()

    if args.daemon:
        asyncio.run(daemon(interval_min=args.interval))
        return 0

    report: dict = {"timestamp": _utc()}
    if args.scan_windows:
        report["windows"] = scan_windows()
    if args.scan_tenet5:
        report["tenet5"] = scan_tenet5_linux()
    if args.gap:
        report["gap"] = capability_gap()
    if args.file_backlog:
        added = file_gap_as_backlog()
        report["backlog_tasks_added"] = added
    if not (args.scan_windows or args.scan_tenet5 or args.gap or args.file_backlog):
        # Default: do all three
        report["windows"] = scan_windows()
        report["tenet5"]  = scan_tenet5_linux()
        report["gap"]     = capability_gap()

    if args.classify:
        for key in ("windows", "tenet5"):
            if key in report and "findings" in report[key]:
                report[key]["findings"] = asyncio.run(
                    classify_findings(report[key]["findings"])
                )

    if args.publish:
        pub = asyncio.run(publish_findings(report))
        report["_publish"] = pub

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_summary(report)
    return 0


def _print_summary(report: dict) -> None:
    print(f"── LIRIL OS agent report @ {report.get('timestamp','?')} ──")
    if "windows" in report:
        w = report["windows"]
        print(f"\n[WINDOWS] listen ports: {len(w.get('listen_ports', []))}  "
              f"services: {len(w.get('services_by_status', []))}  "
              f"reboot_pending: {w.get('reboot_pending')}")
        for f in w.get("findings", []):
            axis = (f.get("npu_classification") or {}).get("axis", "-")
            print(f"  [{f['severity']:8s}] {f['category']:14s} {f['summary']}  (axis={axis})")

    if "tenet5" in report:
        t = report["tenet5"]
        print(f"\n[TENET5-LINUX] containers: {len(t.get('containers', []))}  "
              f"guest_os_api: {t.get('guest_os_api',{}).get('up')}")
        for f in t.get("findings", []):
            axis = (f.get("npu_classification") or {}).get("axis", "-")
            print(f"  [{f['severity']:8s}] {f['category']:14s} {f['summary']}  (axis={axis})")

    if "gap" in report:
        g = report["gap"]
        print(f"\n[CAPABILITY GAP] Windows→tenet5 parity: {g.get('total_gaps',0)} open areas")
        for e in g.get("entries", [])[:6]:
            print(f"  {e['area']:13s} — {e['windows_feature'][:62]}")

    if "backlog_tasks_added" in report:
        print(f"\n[BACKLOG] added {report['backlog_tasks_added']} WS-OS-* tasks")


if __name__ == "__main__":
    import sys
    sys.exit(main())
