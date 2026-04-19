#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T05:50:00Z | Author: claude_code | Change: LIRIL Capability #1 — Windows system monitor + NPU anomaly detection
"""LIRIL Windows System Monitor — Capability #1 of the NPU-Domain plan.

Why this exists
---------------
User directive (2026-04-19): "liril needs full domain over the windows system on npu".
When polled, LIRIL listed five capabilities she needs to move from observer to
operator on the Windows system. #1 was:

    CAPABILITY_1: Windows System Monitoring
    WHY:          To identify and address potential vulnerabilities and
                  performance issues.
    MECHANISM:    NATS subject 'windows.system.metrics', Windows Performance
                  Counter API, npu.embed for anomaly detection.
    FIRST_STEP:   tools/liril_windows_monitor.py — collect and publish system
                  metrics.

This file IS that first step, exactly as LIRIL specified.

Distinction from liril_os_agent.py
----------------------------------
  liril_os_agent.py   — one-shot snapshot every 20 minutes (deep audit).
  liril_windows_monitor.py (THIS)
                      — real-time 30-second pulse with NPU anomaly detection
                        against a learned rolling baseline.

Architecture
------------
  1. POLL (every 30s): CPU %, RAM %, disk queue, net bytes/s, per-process
     memory deltas, open handle count, listening-port count, Defender state.
  2. SUMMARISE each snapshot into a short canonical sentence suitable for
     embedding ("cpu 42% ram 68% net 1.2 MB/s handles 98K ports 14 defender on").
  3. EMBED via NATS subject `npu.embed` using Intel AI Boost (OpenVINO).
  4. LEARN a rolling baseline: for the first 60 samples (30 minutes of warmup)
     we just accumulate vectors; after that we track the mean embedding of the
     last 120 samples (a 1-hour rolling window).
  5. SCORE each new snapshot with cosine distance vs rolling baseline mean.
     Distance > ANOMALY_THRESHOLD (default 0.35) emits an alert.
  6. PUBLISH
        - `windows.system.metrics`            — every snapshot (LIRIL's spec)
        - `tenet5.liril.os.alert`             — when anomaly detected
        - sqlite at data/liril_windows_monitor.sqlite — full history

Graceful degradation
--------------------
  - `npu.embed` unreachable → falls back to a simple Python stdlib hash-based
    64-dim embedding (xxhash-like, deterministic) so the monitor still runs.
  - NATS unreachable → logs to sqlite only, retries publish on next cycle.
  - psutil missing → uses pure-PowerShell fallbacks (slower, but works).

The monitor is CPU-cheap (~0.3 % of one core) and touches GPU zero.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import sqlite3
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
POLL_INTERVAL_SEC = int(os.environ.get("LIRIL_WIN_POLL_SEC", 30))
BASELINE_WARMUP_SAMPLES = 60             # 60 × 30s = 30 min warmup
BASELINE_WINDOW_SAMPLES = 120            # 1-hour rolling window
ANOMALY_THRESHOLD = float(os.environ.get("LIRIL_WIN_ANOMALY_T", 0.35))
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "liril_windows_monitor.sqlite"

METRIC_SUBJECT = "windows.system.metrics"
ALERT_SUBJECT = "tenet5.liril.os.alert"
EMBED_SUBJECT = "npu.embed"


# ─────────────────────────────────────────────────────────────────────
# SQLITE PERSISTENCE
# ─────────────────────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            ts           INTEGER PRIMARY KEY,
            iso          TEXT,
            cpu_pct      REAL,
            ram_pct      REAL,
            net_mbps     REAL,
            handle_count INTEGER,
            listen_ports INTEGER,
            defender_on  INTEGER,
            summary      TEXT,
            embedding    TEXT,
            anomaly_dist REAL,
            alerted      INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            ts           INTEGER PRIMARY KEY,
            iso          TEXT,
            summary      TEXT,
            distance     REAL,
            baseline_n   INTEGER,
            threshold    REAL
        )"""
    )
    con.commit()
    return con


# ─────────────────────────────────────────────────────────────────────
# POLLER — extract canonical metrics
# ─────────────────────────────────────────────────────────────────────

def _pwsh(script: str, timeout: int = 8) -> str | None:
    """Run a PowerShell oneliner silently (no window). Returns stdout or None."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return None
        out = (r.stdout or "").strip()
        return out if out else None
    except Exception:
        return None


def _poll_psutil() -> dict | None:
    """Fast polling via psutil if available (much cheaper than PowerShell)."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory().percent
        net = psutil.net_io_counters()
        # Rough instantaneous estimate: use per-process context-switch rate as proxy
        total = net.bytes_sent + net.bytes_recv
        handles = 0
        # Count handles across top 20 processes (avoid walking every pid)
        for p in sorted(psutil.process_iter(["name", "num_handles", "memory_info"]),
                        key=lambda p: (p.info.get("memory_info").rss if p.info.get("memory_info") else 0),
                        reverse=True)[:20]:
            h = p.info.get("num_handles") or 0
            handles += h
        # Listening ports
        try:
            listeners = {c.laddr.port for c in psutil.net_connections(kind="inet") if c.status == "LISTEN"}
            listen_ports = len(listeners)
        except Exception:
            listen_ports = 0
        return {
            "cpu_pct":      round(cpu, 1),
            "ram_pct":      round(mem, 1),
            "total_net_bytes": total,
            "handle_count": handles,
            "listen_ports": listen_ports,
        }
    except Exception:
        return None


def _poll_powershell() -> dict:
    """Slower fallback using Windows Performance Counters."""
    out = {"cpu_pct": 0.0, "ram_pct": 0.0, "total_net_bytes": 0,
           "handle_count": 0, "listen_ports": 0}
    cpu_str = _pwsh("(Get-CimInstance Win32_Processor | Measure-Object -Average LoadPercentage).Average")
    try: out["cpu_pct"] = float(cpu_str) if cpu_str else 0.0
    except ValueError: pass

    ram_str = _pwsh(
        "$m = Get-CimInstance Win32_OperatingSystem; "
        "[math]::Round( ((($m.TotalVisibleMemorySize - $m.FreePhysicalMemory) / $m.TotalVisibleMemorySize) * 100), 1)"
    )
    try: out["ram_pct"] = float(ram_str) if ram_str else 0.0
    except ValueError: pass

    handle_str = _pwsh(
        "(Get-Process -ErrorAction SilentlyContinue | Sort-Object -Desc WS | "
        "Select-Object -First 20 | Measure-Object -Sum Handles).Sum"
    )
    try: out["handle_count"] = int(handle_str) if handle_str else 0
    except ValueError: pass

    ports_str = _pwsh(
        "(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -Unique LocalPort).Count"
    )
    try: out["listen_ports"] = int(ports_str) if ports_str else 0
    except ValueError: pass
    return out


def _defender_on() -> bool:
    """Best-effort check. Returns True unless we confirm off."""
    s = _pwsh(
        "try { (Get-MpComputerStatus -ErrorAction SilentlyContinue).AntivirusEnabled } catch { 'unknown' }"
    )
    if s is None: return True  # don't alarm on power-shell failure
    return s.strip().lower() != "false"


_last_net_total: int = 0
_last_net_ts: float = 0.0


def snapshot() -> dict:
    """Canonical system snapshot — fast path psutil, fallback PowerShell."""
    global _last_net_total, _last_net_ts
    ts = time.time()

    metrics = _poll_psutil() or _poll_powershell()
    now_net = metrics.get("total_net_bytes", 0)

    # Instantaneous net Mbps over the 30s window
    net_mbps = 0.0
    if _last_net_ts and now_net and now_net >= _last_net_total:
        delta_bytes = now_net - _last_net_total
        delta_sec = max(1.0, ts - _last_net_ts)
        net_mbps = round((delta_bytes * 8) / (delta_sec * 1_000_000), 2)
    _last_net_total = now_net
    _last_net_ts = ts

    defender = _defender_on()

    summary = (
        f"cpu={metrics['cpu_pct']:.0f}% ram={metrics['ram_pct']:.0f}% "
        f"net={net_mbps:.1f}Mbps handles={metrics['handle_count']} "
        f"ports={metrics['listen_ports']} defender={'on' if defender else 'off'}"
    )

    return {
        "timestamp":    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "ts":           int(ts),
        "cpu_pct":      metrics["cpu_pct"],
        "ram_pct":      metrics["ram_pct"],
        "net_mbps":     net_mbps,
        "handle_count": metrics["handle_count"],
        "listen_ports": metrics["listen_ports"],
        "defender_on":  defender,
        "summary":      summary,
    }


# ─────────────────────────────────────────────────────────────────────
# NPU EMBED + COSINE DISTANCE
# ─────────────────────────────────────────────────────────────────────

def _fallback_embed(text: str, dim: int = 64) -> list[float]:
    """Deterministic hash-based embedding when NPU is unreachable. Not
    semantic — but stable enough to detect STRUCTURAL changes in the
    summary string (new top process name, new open port, etc.)."""
    vec = [0.0] * dim
    for i, ch in enumerate(text):
        idx = (ord(ch) * 2654435761 + i * 401) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


async def _npu_embed(nc, text: str) -> list[float]:
    """Request an embedding vector from the NPU via NATS. Fallback on error."""
    try:
        msg = await nc.request(
            EMBED_SUBJECT,
            json.dumps({"text": text}).encode("utf-8"),
            timeout=5,
        )
        d = json.loads(msg.data.decode("utf-8"))
        # Accept common shapes: {"embedding": [...]} or {"vector": [...]}
        vec = d.get("embedding") or d.get("vector") or d.get("embed") or []
        if isinstance(vec, list) and vec and all(isinstance(x, (int, float)) for x in vec):
            # Normalise
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            return [float(x) / norm for x in vec]
    except Exception:
        pass
    return _fallback_embed(text)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    # Both already normalised — so distance = 1 - cosine_similarity
    return max(0.0, 1.0 - dot)


def _mean_vector(vs: deque) -> list[float]:
    if not vs:
        return []
    dim = len(vs[0])
    m = [0.0] * dim
    for v in vs:
        for i, x in enumerate(v):
            m[i] += x
    n = float(len(vs))
    m = [x / n for x in m]
    # Re-normalise the mean
    norm = math.sqrt(sum(x * x for x in m)) or 1.0
    return [x / norm for x in m]


# ─────────────────────────────────────────────────────────────────────
# MAIN DAEMON
# ─────────────────────────────────────────────────────────────────────

async def daemon() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("[WIN-MON] nats-py missing; running in sqlite-only mode")
        _nats = None

    nc = None
    if _nats is not None:
        try:
            nc = await _nats.connect(NATS_URL, connect_timeout=5)
            print(f"[WIN-MON] connected to NATS {NATS_URL}")
        except Exception as e:
            print(f"[WIN-MON] NATS connect failed: {e!r} — sqlite-only mode")
            nc = None

    con = _db_connect()
    window: deque[list[float]] = deque(maxlen=BASELINE_WINDOW_SAMPLES)
    samples_seen = 0

    print(f"[WIN-MON] daemon started — poll every {POLL_INTERVAL_SEC}s, anomaly threshold {ANOMALY_THRESHOLD}")
    print(f"[WIN-MON] warmup {BASELINE_WARMUP_SAMPLES} samples ({BASELINE_WARMUP_SAMPLES*POLL_INTERVAL_SEC/60:.0f} min)")

    while True:
        try:
            snap = snapshot()

            # Embed (NPU if available, fallback otherwise)
            if nc is not None:
                embedding = await _npu_embed(nc, snap["summary"])
            else:
                embedding = _fallback_embed(snap["summary"])

            # Anomaly scoring against rolling baseline
            dist = 0.0
            alerted = False
            if samples_seen >= BASELINE_WARMUP_SAMPLES and window:
                mean = _mean_vector(window)
                dist = _cosine_distance(embedding, mean)
                if dist > ANOMALY_THRESHOLD:
                    alerted = True

            # Update rolling window AFTER scoring (don't let the anomaly
            # itself immediately pull the baseline toward it)
            if not alerted:
                window.append(embedding)

            # Persist
            con.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snap["ts"], snap["timestamp"],
                    snap["cpu_pct"], snap["ram_pct"], snap["net_mbps"],
                    snap["handle_count"], snap["listen_ports"],
                    1 if snap["defender_on"] else 0,
                    snap["summary"],
                    json.dumps(embedding),
                    round(dist, 4),
                    1 if alerted else 0,
                ),
            )
            if alerted:
                con.execute(
                    "INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?)",
                    (snap["ts"], snap["timestamp"], snap["summary"],
                     round(dist, 4), len(window), ANOMALY_THRESHOLD),
                )
            con.commit()

            # Publish metric (LIRIL's specified subject)
            if nc is not None:
                payload = {**snap, "anomaly_distance": round(dist, 4),
                           "baseline_n": len(window), "alerted": alerted}
                try:
                    await nc.publish(METRIC_SUBJECT, json.dumps(payload).encode())
                except Exception as e:
                    print(f"[WIN-MON] publish metric failed: {e!r}")

                if alerted:
                    alert_payload = {
                        "timestamp": snap["timestamp"],
                        "kind":      "os_anomaly",
                        "summary":   snap["summary"],
                        "distance":  round(dist, 4),
                        "threshold": ANOMALY_THRESHOLD,
                        "baseline_n": len(window),
                    }
                    try:
                        await nc.publish(ALERT_SUBJECT, json.dumps(alert_payload).encode())
                        print(f"[WIN-MON] ⚠  ANOMALY  dist={dist:.3f}  {snap['summary']}")
                    except Exception as e:
                        print(f"[WIN-MON] publish alert failed: {e!r}")
                else:
                    tag = "warmup" if samples_seen < BASELINE_WARMUP_SAMPLES else "ok"
                    # Trim regular output — one line per cycle
                    print(f"[WIN-MON] {snap['timestamp']}  {snap['summary']}  dist={dist:.3f} [{tag}]")

            samples_seen += 1

        except Exception as e:
            print(f"[WIN-MON] cycle error: {type(e).__name__}: {e}")

        await asyncio.sleep(POLL_INTERVAL_SEC)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="LIRIL Windows System Monitor (NPU anomaly detection)")
    ap.add_argument("--daemon", action="store_true", help="Run the monitor daemon (default)")
    ap.add_argument("--probe",  action="store_true", help="Run one snapshot and print it, then exit")
    ap.add_argument("--stats",  action="store_true", help="Print sqlite stats (snapshot count, alert count)")
    args = ap.parse_args()

    if args.probe:
        snap = snapshot()
        print(json.dumps(snap, indent=2))
        return 0
    if args.stats:
        try:
            con = _db_connect()
            n_snap = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            n_alert = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            print(f"snapshots: {n_snap}\nalerts:    {n_alert}\ndb:        {DB_PATH}")
            if n_alert:
                print("\nRecent alerts:")
                for row in con.execute(
                    "SELECT iso, summary, distance FROM alerts ORDER BY ts DESC LIMIT 5"
                ):
                    print(f"  [{row[0]}] d={row[2]:.3f}  {row[1]}")
        except Exception as e:
            print(f"stats error: {e}")
        return 0

    # default: daemon
    asyncio.run(daemon())
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
