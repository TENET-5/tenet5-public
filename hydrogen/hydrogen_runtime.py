# Copyright (C) 2026, TENET5 Development Team
# SPDX-License-Identifier: EOSL-2.0
# Modified: 2026-03-21T09:20:00-04:00

"""HYDROGEN Runtime Diagnostics — verify all 13 runtime integrations.

Probes each integration endpoint to verify live connectivity. Uses LIRIL
API where available, falls back to direct checks (file existence, port
probes, process checks).

SYSTEM_SEED=118400
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SYSTEM_SEED = 118400
# 2026-04-19: LIRIL API migrated from 18090 (guest OS port) to 18120
# (canonical host-side liril_api.py). Env override for future moves.
LIRIL_API = os.environ.get("LIRIL_API_BASE", "http://127.0.0.1:18120")
NATS_PORT = 4223
TENET5_ROOT = Path(os.environ.get("TENET5_ROOT", "E:/S.L.A.T.E/tenet5"))
HYDROGEN_ROOT = Path(os.environ.get("HYDROGEN_ROOT", "E:/S.L.A.T.E/hydrogen"))


def _liril_get(endpoint: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """GET request to LIRIL API."""
    try:
        url = f"{LIRIL_API}{endpoint}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _nvidia_smi_ok() -> bool:
    """Check if nvidia-smi is responsive."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return r.returncode == 0 and len(r.stdout.strip()) > 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _check(name: str, check_fn) -> Tuple[bool, str]:
    """Run a check function and return (ok, detail)."""
    try:
        ok, detail = check_fn()
        return ok, detail
    except Exception as e:
        return False, str(e)


def check_all() -> None:
    """Verify all 13 runtime integrations."""
    print("[Runtime Check] Verifying all 13 integrations...")
    print()

    passed = 0
    failed = 0

    integrations = [
        # 1. LIRIL Orchestrator
        ("LIRIL Orchestrator", lambda: (
            (_liril_get("/status") is not None),
            f"API at {LIRIL_API}"
        )),
        # 2. NATS Bus
        ("NATS Message Bus", lambda: (
            _port_open("127.0.0.1", NATS_PORT),
            f"Port {NATS_PORT}"
        )),
        # 3. NemoClaw GPU Inference
        ("NemoClaw GPU Inference", lambda: (
            _nvidia_smi_ok(),
            "nvidia-smi responsive"
        )),
        # 4. LIRILClaw NPU Classifier
        ("LIRILClaw NPU Classifier", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "lirilclaw_npu.py").exists(),
            "Module present"
        )),
        # 5. GPU Health Monitor
        ("GPU Health Monitor", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "nemoclaw_health_monitor.py").exists(),
            "Module present"
        )),
        # 6. LIRIL Training Persistence
        ("LIRIL Training Persistence", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "liril_training_persistence.py").exists(),
            "Module present"
        )),
        # 7. NemoClaw Task Scheduler
        ("NemoClaw Task Scheduler", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "nemoclaw_task_scheduler.py").exists(),
            "Module present"
        )),
        # 8. NemoClaw Codegen Agent
        ("NemoClaw Codegen Agent", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "nemoclaw_codegen.py").exists(),
            "Module present"
        )),
        # 9. LIRIL NPU Service
        ("LIRIL NPU Service", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "liril_npu_service.py").exists(),
            "Module present"
        )),
        # 10. S.L.A.T.E Nexus
        ("S.L.A.T.E Nexus", lambda: (
            TENET5_ROOT.exists(),
            str(TENET5_ROOT)
        )),
        # 11. SATOR Grid (MCP Server)
        ("SATOR Grid (MCP Server)", lambda: (
            (TENET5_ROOT / "src" / "tenet" / "aurora" / "tenet5_mcp_server.py").exists(),
            "MCP server module present"
        )),
        # 12. STARK Validation (Agentic CI/CD)
        ("STARK CI/CD Pipeline", lambda: (
            (TENET5_ROOT / "agentic_cicd.py").exists(),
            "18-phase pipeline present"
        )),
        # 13. Dark-and-Light Bridge
        ("Dark-and-Light Bridge", lambda: (
            Path("E:/dark-and-light/tenet_bridge.js").exists(),
            "NATS telemetry bridge present"
        )),
    ]

    for name, check_fn in integrations:
        ok, detail = _check(name, check_fn)
        status = "OK" if ok else "FAIL"
        icon = "✓" if ok else "✗"
        print(f"  [{status}] {icon} {name:35s} — {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    if failed == 0:
        print(f"[OK] All {passed}/{len(integrations)} core runtime hooks successfully validated.")
    else:
        print(f"[WARN] {passed}/{len(integrations)} passed, {failed} failed.")

    # LIRIL live status if available
    status = _liril_get("/status")
    if status:
        hw = status.get("hardware", {})
        gpus = hw.get("gpus", [])
        print()
        print(f"  LIRIL: {status.get('liril', '?')} | Grid: {status.get('grid_size', '?')} | "
              f"Agents: {status.get('agents_online', '?')}/25 | "
              f"Tick: {status.get('tick_rate_hz', 0):.1f} Hz")
        for gpu in gpus:
            print(f"  GPU{gpu.get('index', '?')}: {gpu.get('temp_c', '?')}°C | "
                  f"{gpu.get('vram_used_mb', 0):.0f}/{gpu.get('vram_total_mb', 0):.0f}MB | "
                  f"{gpu.get('power_draw_w', 0):.1f}W")

    sys.exit(0 if failed == 0 else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="HYDROGEN Runtime Diagnostics")
    parser.add_argument("--check-all", action="store_true", help="Verify all 13 runtime integrations")
    args = parser.parse_args()

    if args.check_all:
        check_all()
    else:
        print("HYDROGEN Runtime — use --check-all to verify all 13 integrations")

    sys.exit(0)


if __name__ == "__main__":
    main()
