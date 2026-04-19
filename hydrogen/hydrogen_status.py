# Copyright (C) 2026, TENET5 Development Team
# SPDX-License-Identifier: EOSL-2.0
# Modified: 2026-03-21T10:50:00-04:00

"""HYDROGEN Status Protocol — live system health via LIRIL API.

Queries the LIRIL MCP API at 127.0.0.1:18090 for real-time system
status including GPU health, training samples, loom verification,
and agent availability. Falls back to offline stub when LIRIL is
unreachable.

SYSTEM_SEED=118400
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import os as _os
from typing import Any, Dict, Optional

# 2026-04-19: LIRIL API migrated from 18090 (guest OS) to 18120 (liril_api.py)
LIRIL_API = _os.environ.get("LIRIL_API_BASE", "http://127.0.0.1:18120")
SYSTEM_SEED = 118400


def _liril_get(endpoint: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """GET request to LIRIL API with timeout."""
    try:
        url = f"{LIRIL_API}{endpoint}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _liril_post(endpoint: str, data: Dict[str, Any], timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """POST request to LIRIL API with timeout."""
    try:
        url = f"{LIRIL_API}{endpoint}"
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def quick_status() -> None:
    """Quick system health check — queries LIRIL for live status."""
    # Try LIRIL status endpoint
    status = _liril_get("/status")

    if status:
        # ── Live LIRIL status ──
        hw = status.get("hardware", {})
        gpus = hw.get("gpus", [])
        agents = status.get("agents_online", 0)
        training = status.get("training_samples", 0)
        tick = status.get("tick_rate_hz", 0)

        # Determine mode
        max_temp = hw.get("max_gpu_temp_c", 0) or 0
        ram_pct = hw.get("ram_pct", 0) or 0
        if max_temp > 85 or ram_pct > 95:
            mode = "critical"
        elif max_temp > 75 or ram_pct > 90:
            mode = "degraded"
        else:
            mode = "normal"

        print(f"**TONY STARKINATOR | mode: {mode} | dashboard: {tick:.0f} | agents: {agents:02d}/25 online | 'The suit is online.'**")
        print(f"[OK] Genesis: H (SEED={SYSTEM_SEED})")
        print(f"[OK] LIRIL: ONLINE — {status.get('position', 'ETHICS×SEED')}")
        print(f"[OK] Training: {training} samples ({', '.join(f'{k}:{v}' for k, v in status.get('domain_counts', {}).items())})")
        print(f"[OK] Tick: {tick:.1f} Hz | Grid: {status.get('grid_size', '5×5')}")

        # GPU health
        for gpu in gpus:
            idx = gpu.get("index", "?")
            name = gpu.get("name", "Unknown")
            temp = gpu.get("temp_c", 0)
            vram = gpu.get("vram_used_mb", 0)
            vram_total = gpu.get("vram_total_mb", 0)
            power = gpu.get("power_draw_w", 0)
            util = gpu.get("utilization_pct", 0)
            health = "OK" if temp < 80 else "WARN" if temp < 90 else "CRIT"
            print(f"[{health}] GPU{idx}: {name} | {temp}°C | {vram:.0f}/{vram_total:.0f}MB ({util}%) | {power:.1f}W")

        # System resources
        print(f"[OK] RAM: {hw.get('ram_used_gb', 0):.1f}/{hw.get('ram_total_gb', 0):.1f}GB ({ram_pct:.0f}%)")
        print(f"[OK] CPU: {hw.get('cpu_pct', 0):.0f}% @ {hw.get('cpu_freq_mhz', 0):.0f}MHz")

        # Providers
        providers = status.get("providers", {})
        live = [k for k, v in providers.items() if isinstance(v, dict) and v.get("available")]
        print(f"[OK] Providers: {', '.join(live) if live else 'none'}")

    else:
        # ── Offline fallback ──
        print("**TONY STARKINATOR | mode: degraded | dashboard: -- | agents: ??/25 online | 'LIRIL unreachable.'**")
        print("[WARN] Genesis: H (SEED=118400)")
        print("[FAIL] LIRIL: OFFLINE — cannot reach 127.0.0.1:18090")
        print("[INFO] Run: python -m tenet.aurora.tenet5_mcp_server to start LIRIL")

    sys.exit(0)


def full_status() -> None:
    """Full system health — status + loom + session summary."""
    quick_status()

    print()
    print("── LOOM Verification ──")
    loom = _liril_post("/tools/liril_loom_verify", {})
    if loom:
        print(f"  Constant: {loom.get('loom_constant', '?')} ({'OK' if loom.get('constant_ok') else 'FAIL'})")
        print(f"  Axes: x={'✓' if loom.get('axes', {}).get('x') else '✗'} "
              f"y={'✓' if loom.get('axes', {}).get('y') else '✗'} "
              f"z={'✓' if loom.get('axes', {}).get('z') else '✗'}")
        print(f"  Eigen: sum={loom.get('eigen_sum', '?')}, aligned={'✓' if loom.get('eigen_aligned') else '✗'}")
        print(f"  Status: {loom.get('status', 'UNKNOWN')}")
    else:
        print("  [SKIP] LIRIL not reachable")

    print()
    print("── Session Summary ──")
    session = _liril_post("/tools/liril_session_summary", {"last_n": 100})
    if session:
        print(f"  DB: {session.get('training_db_total', 0)} samples")
        print(f"  Domains: {session.get('domain_distribution', {})}")
        print(f"  PCG entropy: {session.get('pcg_entropy_bits', 0):.4f} bits")
        print(f"  LOOM verified: {'✓' if session.get('loom_verified') else '✗'}")
    else:
        print("  [SKIP] LIRIL not reachable")


def main() -> None:
    parser = argparse.ArgumentParser(description="HYDROGEN Status Protocol")
    parser.add_argument("--quick", action="store_true", help="Quick system health check")
    parser.add_argument("--full", action="store_true", help="Full status with loom + session")
    args = parser.parse_args()

    if args.full:
        full_status()
    elif args.quick:
        quick_status()
    else:
        quick_status()


if __name__ == "__main__":
    main()
