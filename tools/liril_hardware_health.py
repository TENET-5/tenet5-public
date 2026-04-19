#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T16:30:00Z | Author: claude_code | Change: Grok round 6 — WAL + busy_timeout + immediate stuck-fan override at >=90°C (bypass 3-cycle counter)
"""LIRIL Hardware Health Telemetry — Capability #8 of the post-NPU plan.

LIRIL (poll 2026-04-19): "Monitors GPU/NPU/thermals/SMART metrics to predict
and prevent hardware failures."  Severity if missing = high.

Why this matters after Cap#10
-----------------------------
Cap#10 (fail-safe escalation) is a reactive gate — it only escalates when
something publishes an incident. Cap#8 is one of the *producers* that keeps
the gate useful. Without Cap#8, GPU thermal runaway or a failing disk would
never trip fse's level, and Cap#2/#3/#4 would happily continue mutating the
host through a dying machine.

What we poll (every 30s by default)
-----------------------------------
  GPU (per device, via nvidia-smi):
    temperature_gpu, utilization.gpu, memory.used/total, power.draw, fan.speed
  NPU (Intel AI Boost):
    device presence via OpenVINO core.get_property (NPU-only, guarded — never
    call available_devices per CLAUDE.md rule)
  CPU:
    package temperature via WMI MSAcpi_ThermalZoneTemperature (no admin needed
    on most systems; silently degrades if locked)
  Storage:
    Get-PhysicalDisk → HealthStatus, OperationalStatus, MediaType, Wear
    Smart.Status where available
  Memory/Swap:
    psutil.virtual_memory + swap_memory (for sudden-spike incidents)

Thresholds → fse severity mapping
---------------------------------
  GPU temp      >=  95°C  → critical
  GPU temp      >=  85°C  → high
  GPU fan stuck (RPM=0) with temp > 60°C for 3+ cycles → high
  NPU device missing mid-run → high
  Disk HealthStatus != Healthy → critical
  Disk MediaFailure predicted → critical
  CPU package temp >= 95°C → critical
  CPU package temp >= 85°C → high
  swap in-use > 80% sustained 3+ cycles → high (memory pressure signal)

Outputs
-------
  NATS:
    windows.hardware.health.metrics   — full snapshot every cycle
    tenet5.liril.failsafe.incident    — one incident per threshold crossing
  local sqlite:
    data/liril_hardware_health.sqlite — last 7 days of snapshots

CLI
---
  --snapshot     One-shot publish and print a snapshot
  --daemon       Run the 30s loop forever
  --thresholds   Print the threshold table and exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NATS_URL        = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
METRICS_SUBJECT = "windows.hardware.health.metrics"
INCIDENT_SUBJECT = "tenet5.liril.failsafe.incident"
POLL_INTERVAL_SEC = float(os.environ.get("LIRIL_HW_INTERVAL", "30"))
STUCK_FAN_CYCLES = 3
SWAP_PRESSURE_CYCLES = 3

GPU_TEMP_HIGH      = 85
GPU_TEMP_CRITICAL  = 95
CPU_TEMP_HIGH      = 85
CPU_TEMP_CRITICAL  = 95
SWAP_PCT_HIGH      = 80.0
# Grok round 6: stuck-fan cycle counter (STUCK_FAN_CYCLES=3 @ 30s tick = 90s
# before action) is too slow for a 5070 Ti — the card can thermal-runaway in
# under 30s at 95°C. This threshold triggers an IMMEDIATE high-severity
# incident the first time we see fan=0% at ≥ this temp, no counter required.
GPU_STUCK_FAN_IMMEDIATE_C = 90

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH  = DATA_DIR / "liril_hardware_health.sqlite"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _db() -> sqlite3.Connection:
    # Grok round 6: Cap#8 writes every 30s and the INSERT happens while Cap#6
    # may be concurrently reading snapshots for its history. WAL + 15s timeout
    # prevents the 'database is locked' storm the other capabilities saw.
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts       REAL PRIMARY KEY,
            payload  TEXT NOT NULL
        )
    """)
    c.commit()
    return c


# ─────────────────────────────────────────────────────────────────────
# GPU — nvidia-smi
# ─────────────────────────────────────────────────────────────────────

_NVSMI_FIELDS = (
    "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,"
    "power.draw,fan.speed,pstate"
)

def _gpus() -> list[dict]:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + _NVSMI_FIELDS,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return []
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout:
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        def _num(s: str, default=None):
            try:
                return float(s)
            except Exception:
                return default
        out.append({
            "index":         int(_num(parts[0], 0)),
            "name":          parts[1],
            "temp_c":        _num(parts[2]),
            "util_pct":      _num(parts[3]),
            "mem_used_mb":   _num(parts[4]),
            "mem_total_mb":  _num(parts[5]),
            "power_w":       _num(parts[6]),
            "fan_pct":       _num(parts[7]),
            "pstate":        parts[8],
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# NPU — Intel AI Boost via OpenVINO
# ─────────────────────────────────────────────────────────────────────

def _npu() -> dict:
    try:
        import openvino as ov  # type: ignore
        core = ov.Core()
        # CLAUDE.md rule: NEVER call available_devices — probe NPU directly
        try:
            name = core.get_property("NPU", "FULL_DEVICE_NAME")
        except Exception:
            return {"present": False, "error": "NPU property unreadable"}
        return {"present": True, "name": str(name)}
    except ImportError:
        return {"present": None, "error": "openvino not installed"}
    except Exception as e:
        return {"present": False, "error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────
# CPU TEMP — WMI
# ─────────────────────────────────────────────────────────────────────

def _cpu_temp_c() -> float | None:
    """Query MSAcpi_ThermalZoneTemperature in root/wmi. Returns highest zone
    temperature in Celsius, or None if unavailable."""
    script = (
        "try { "
        "$z = Get-WmiObject -Namespace 'root/wmi' -Class MSAcpi_ThermalZoneTemperature "
        "-ErrorAction Stop; "
        "$maxK = ($z | Measure-Object -Property CurrentTemperature -Maximum).Maximum; "
        "if ($null -ne $maxK) { [math]::Round(($maxK / 10) - 273.15, 1) } else { 'none' } "
        "} catch { 'error' }"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        if not out or out in ("none", "error"):
            return None
        return float(out)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# STORAGE — Get-PhysicalDisk
# ─────────────────────────────────────────────────────────────────────

def _disks() -> list[dict]:
    script = (
        "Get-PhysicalDisk | Select-Object FriendlyName,SerialNumber,"
        "HealthStatus,OperationalStatus,MediaType,BusType,"
        "@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}} "
        "| ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    out = (r.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    disks = []
    for d in data:
        op = d.get("OperationalStatus")
        if isinstance(op, list):
            op = ",".join(str(x) for x in op)
        disks.append({
            "friendly_name":      d.get("FriendlyName", ""),
            "serial":             (d.get("SerialNumber") or "").strip(),
            "health_status":      d.get("HealthStatus", ""),
            "operational_status": op or "",
            "media_type":         d.get("MediaType", ""),
            "bus_type":           d.get("BusType", ""),
            "size_gb":            d.get("SizeGB"),
        })
    return disks


# ─────────────────────────────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────────────────────────────

def _memory() -> dict:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "ram_total_gb":  round(vm.total / 1024**3, 1),
            "ram_used_pct":  round(vm.percent, 1),
            "swap_total_gb": round(sw.total / 1024**3, 1) if sw.total else 0.0,
            "swap_used_pct": round(sw.percent, 1) if sw.total else 0.0,
        }
    except ImportError:
        return {}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────
# SNAPSHOT + THRESHOLD EVALUATION
# ─────────────────────────────────────────────────────────────────────

# Per-device rolling state for "3+ cycles" thresholds
_state_stuck_fan: dict[int, int] = {}   # gpu index → consecutive hot+stuck cycles
_state_swap_hot: int = 0


def _snapshot() -> dict:
    return {
        "ts":       _utc(),
        "host":     os.environ.get("COMPUTERNAME", ""),
        "gpus":     _gpus(),
        "npu":      _npu(),
        "cpu_c":    _cpu_temp_c(),
        "disks":    _disks(),
        "memory":   _memory(),
    }


def _evaluate_thresholds(snap: dict) -> list[dict]:
    """Return list of incidents to file. Each: {severity, source, message, data}."""
    global _state_swap_hot
    incidents: list[dict] = []

    # GPU thermals + stuck fans
    for g in snap.get("gpus", []):
        idx = g.get("index", 0)
        t = g.get("temp_c")
        fan = g.get("fan_pct")
        if t is None:
            continue
        if t >= GPU_TEMP_CRITICAL:
            incidents.append({
                "severity": "critical",
                "source":   "hardware_health",
                "message":  f"GPU{idx} ({g.get('name','')}) temp {t}°C >= {GPU_TEMP_CRITICAL}°C",
                "data":     g,
            })
        elif t >= GPU_TEMP_HIGH:
            incidents.append({
                "severity": "high",
                "source":   "hardware_health",
                "message":  f"GPU{idx} ({g.get('name','')}) temp {t}°C >= {GPU_TEMP_HIGH}°C",
                "data":     g,
            })
        # Stuck fan detection.
        # Grok round 6 (2026-04-19): 5070 Ti can thermal-runaway in < 30s at
        # 95°C; a 3-cycle wait (~90s with 30s tick) is too slow. IMMEDIATE
        # critical incident whenever fan=0 AND temp >= GPU_STUCK_FAN_IMMEDIATE_C
        # (90°C). Fall back to the cycle counter for the milder
        # "hot but not runaway" range.
        if fan is not None and fan <= 0 and t is not None:
            if t >= GPU_STUCK_FAN_IMMEDIATE_C:
                # Emergency: do not wait for the cycle counter
                incidents.append({
                    "severity": "critical",
                    "source":   "hardware_health",
                    "message":  (f"GPU{idx} fan stuck at 0% at {t}°C "
                                 f"(>= {GPU_STUCK_FAN_IMMEDIATE_C}°C immediate override)"),
                    "data":     g,
                })
                _state_stuck_fan[idx] = 0  # reset so counter doesn't double-fire
            elif t > 60:
                _state_stuck_fan[idx] = _state_stuck_fan.get(idx, 0) + 1
                if _state_stuck_fan[idx] >= STUCK_FAN_CYCLES:
                    incidents.append({
                        "severity": "high",
                        "source":   "hardware_health",
                        "message":  f"GPU{idx} fan stuck at 0% for {_state_stuck_fan[idx]} cycles at {t}°C",
                        "data":     g,
                    })
                    _state_stuck_fan[idx] = 0  # reset to avoid retrigger storm
            else:
                _state_stuck_fan[idx] = 0
        else:
            _state_stuck_fan[idx] = 0

    # NPU disappearance
    npu = snap.get("npu") or {}
    if npu.get("present") is False:  # explicit False (None = unknown/not installed)
        incidents.append({
            "severity": "high",
            "source":   "hardware_health",
            "message":  f"NPU unavailable: {npu.get('error','')}",
            "data":     npu,
        })

    # Disks
    for d in snap.get("disks", []):
        hs = (d.get("health_status") or "").lower()
        if hs and hs != "healthy":
            incidents.append({
                "severity": "critical",
                "source":   "hardware_health",
                "message":  f"Disk {d.get('friendly_name','?')} health={d.get('health_status','?')}",
                "data":     d,
            })

    # CPU temp
    cpu_c = snap.get("cpu_c")
    if cpu_c is not None:
        if cpu_c >= CPU_TEMP_CRITICAL:
            incidents.append({
                "severity": "critical",
                "source":   "hardware_health",
                "message":  f"CPU package {cpu_c}°C >= {CPU_TEMP_CRITICAL}°C",
                "data":     {"cpu_c": cpu_c},
            })
        elif cpu_c >= CPU_TEMP_HIGH:
            incidents.append({
                "severity": "high",
                "source":   "hardware_health",
                "message":  f"CPU package {cpu_c}°C >= {CPU_TEMP_HIGH}°C",
                "data":     {"cpu_c": cpu_c},
            })

    # Swap pressure sustained
    mem = snap.get("memory") or {}
    swap_pct = mem.get("swap_used_pct") or 0.0
    if swap_pct >= SWAP_PCT_HIGH:
        _state_swap_hot += 1
        if _state_swap_hot >= SWAP_PRESSURE_CYCLES:
            incidents.append({
                "severity": "high",
                "source":   "hardware_health",
                "message":  f"swap {swap_pct}% sustained for {_state_swap_hot} cycles — memory pressure",
                "data":     mem,
            })
            _state_swap_hot = 0
    else:
        _state_swap_hot = 0

    return incidents


# ─────────────────────────────────────────────────────────────────────
# PUBLISH
# ─────────────────────────────────────────────────────────────────────

async def _publish_once(nc=None) -> dict:
    snap = _snapshot()
    incidents = _evaluate_thresholds(snap)

    # Persist
    try:
        c = _db()
        try:
            # Keep only last 7 days
            cutoff_ts = time.time() - 7 * 86400
            c.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff_ts,))
            c.execute(
                "INSERT OR REPLACE INTO snapshots(ts, payload) VALUES(?, ?)",
                (time.time(), json.dumps(snap, default=str)),
            )
            c.commit()
        finally:
            c.close()
    except Exception as e:
        print(f"[HW-HEALTH] sqlite write failed: {e!r}")

    # NATS
    if nc is None:
        try:
            import nats as _nats
            nc_local = await _nats.connect(NATS_URL, connect_timeout=3)
        except Exception as e:
            print(f"[HW-HEALTH] NATS unavailable: {e!r}")
            return {"snapshot": snap, "incidents": incidents, "published": False}
        try:
            await _publish_payload(nc_local, snap, incidents)
        finally:
            await nc_local.drain()
        return {"snapshot": snap, "incidents": incidents, "published": True}
    else:
        await _publish_payload(nc, snap, incidents)
        return {"snapshot": snap, "incidents": incidents, "published": True}


async def _publish_payload(nc, snap: dict, incidents: list[dict]) -> None:
    try:
        await nc.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
    except Exception as e:
        print(f"[HW-HEALTH] metrics publish failed: {e!r}")
    for inc in incidents:
        try:
            await nc.publish(INCIDENT_SUBJECT, json.dumps({
                "ts": _utc(),
                **inc,
            }, default=str).encode())
            # Also write directly via failsafe lib so it persists even if the
            # failsafe daemon isn't running
            try:
                sys.path.insert(0, str(ROOT / "tools"))
                import liril_fail_safe_escalation as _fse  # type: ignore
                _fse.file_incident_local(
                    inc["severity"], inc["source"], inc["message"], inc.get("data"),
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[HW-HEALTH] incident publish failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# DAEMON
# ─────────────────────────────────────────────────────────────────────

async def _daemon() -> None:
    print(f"[HW-HEALTH] daemon starting — interval {POLL_INTERVAL_SEC:.0f}s")
    try:
        import nats as _nats
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[HW-HEALTH] NATS unavailable at startup: {e!r} — will retry each cycle")
        nc = None

    try:
        while True:
            try:
                if nc is None:
                    try:
                        import nats as _nats
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                    except Exception:
                        nc = None
                result = await _publish_once(nc)
                inc_count = len(result["incidents"])
                if inc_count:
                    print(f"[HW-HEALTH] cycle: {inc_count} incident(s) filed")
            except Exception as e:
                print(f"[HW-HEALTH] cycle error: {type(e).__name__}: {e}")
                try:
                    if nc is not None:
                        await nc.close()
                except Exception:
                    pass
                nc = None
            await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Hardware Health Telemetry — Capability #8")
    ap.add_argument("--snapshot",   action="store_true",
                    help="One-shot snapshot + publish + print")
    ap.add_argument("--daemon",     action="store_true",
                    help="Run the 30s loop forever")
    ap.add_argument("--thresholds", action="store_true",
                    help="Print the threshold table and exit")
    args = ap.parse_args()

    if args.thresholds:
        for k, v in [
            ("GPU_TEMP_HIGH",             GPU_TEMP_HIGH),
            ("GPU_TEMP_CRITICAL",         GPU_TEMP_CRITICAL),
            ("GPU_STUCK_FAN_IMMEDIATE_C", GPU_STUCK_FAN_IMMEDIATE_C),
            ("CPU_TEMP_HIGH",             CPU_TEMP_HIGH),
            ("CPU_TEMP_CRITICAL",         CPU_TEMP_CRITICAL),
            ("SWAP_PCT_HIGH",             SWAP_PCT_HIGH),
            ("STUCK_FAN_CYCLES",          STUCK_FAN_CYCLES),
            ("SWAP_PRESSURE_CYCLES",      SWAP_PRESSURE_CYCLES),
            ("POLL_INTERVAL_SEC",         POLL_INTERVAL_SEC),
        ]:
            print(f"{k:28s} {v}")
        return 0

    if args.snapshot:
        result = asyncio.run(_publish_once(None))
        print(json.dumps(result, indent=2, default=str)[:4000])
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[HW-HEALTH] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
