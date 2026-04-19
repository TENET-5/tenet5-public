#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T15:25:00Z | Author: claude_code | Change: extend DAEMONS to 16 (add journal + api)
"""LIRIL Status Dashboard — one-command health view.

User directive: polish item C from LIRIL's priority poll.

Usage
-----
  liril_status                  colour console dashboard (default)
  liril_status --json           machine-readable JSON
  liril_status --watch          refresh every 5s until Ctrl-C
  liril_status --brief          single-line summary

What it shows (at a glance):
  - LIRIL:   RUNNING / DEGRADED / DOWN
  - Daemons: N/14 alive (+ list of dead ones if any)
  - FSE:     current level + reason
  - Intent:  current category + confidence
  - GPU:     dual RTX 5070 Ti temps + VRAM
  - NATS:    last-5-min rates per subject (from observer DB)
  - Recent:  last 5 incidents, last 3 file alerts

Designed to be the single command Daniel runs to answer "is LIRIL
alive and working right now?" — no NATS queries, no subprocess calls
to capability CLIs (too slow), just sqlite reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DAEMONS = [
    "failsafe", "observer", "windows_monitor", "service_control",
    "process_manager", "driver_manager", "patch_manager", "self_repair",
    "hardware_health", "user_intent", "nemo_server",
    "network_reach", "communication", "file_awareness", "journal", "api",
    "autonomous",
]


# ─────────────────────────────────────────────────────────────────────
# COLOUR (ANSI — works in Windows Terminal and most modern consoles)
# ─────────────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty() and os.environ.get("LIRIL_STATUS_COLOR", "1") != "0"


def _c(code: str, txt: str) -> str:
    if not USE_COLOR:
        return txt
    return f"\x1b[{code}m{txt}\x1b[0m"


GREEN  = lambda s: _c("32", s)
RED    = lambda s: _c("31", s)
YELLOW = lambda s: _c("33", s)
CYAN   = lambda s: _c("36", s)
DIM    = lambda s: _c("2",  s)
BOLD   = lambda s: _c("1",  s)


# ─────────────────────────────────────────────────────────────────────
# DATA COLLECTORS
# ─────────────────────────────────────────────────────────────────────

def _read_pid(name: str) -> int | None:
    p = ROOT / "data" / "liril_supervisor" / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(int(pid))
    except Exception:
        return True  # assume alive if we can't check


def get_daemon_status() -> list[dict]:
    out = []
    for name in DAEMONS:
        pid = _read_pid(name)
        alive = bool(pid) and _pid_alive(pid)
        out.append({"name": name, "pid": pid, "alive": alive})
    return out


def get_fse_level() -> dict:
    """Read fse level + reason from sqlite (no NATS / no subprocess)."""
    db = ROOT / "data" / "liril_failsafe.sqlite"
    if not db.exists():
        return {"level": 0, "level_name": "nominal", "reason": "<no db>"}
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            def _g(k, default=""):
                r = c.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
                return r[0] if r else default
            level = int(_g("level", "0"))
            reason = _g("last_reason", "")
            last_change = float(_g("last_change_ts", "0") or 0)
            dwell = int(time.time() - last_change) if last_change else 0
            # Count recent incidents
            cutoff = time.time() - 600
            n_inc = c.execute(
                "SELECT COUNT(*) FROM incidents WHERE ts >= ?", (cutoff,)
            ).fetchone()[0] or 0
            n_unacked = c.execute(
                "SELECT COUNT(*) FROM incidents WHERE ts >= ? AND acked=0", (cutoff,)
            ).fetchone()[0] or 0
        finally:
            c.close()
    except Exception as e:
        return {"level": 0, "level_name": "unknown", "reason": f"db error: {e}"}
    names = {0: "nominal", 1: "elevated", 2: "alarmed",
             3: "restricted", 4: "safe_mode"}
    return {
        "level":       level,
        "level_name":  names.get(level, "?"),
        "reason":      reason,
        "dwell_sec":   dwell,
        "incidents_10min": int(n_inc),
        "unacked_10min":   int(n_unacked),
    }


def get_intent() -> dict:
    db = ROOT / "data" / "liril_intent.sqlite"
    if not db.exists():
        return {"category": "UNKNOWN", "confidence": 0.0, "ts": None}
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            def _g(k, default=""):
                r = c.execute("SELECT value FROM current WHERE key=?", (k,)).fetchone()
                return r[0] if r else default
            cat = _g("category", "UNKNOWN")
            conf = float(_g("confidence", "0") or 0)
            ts = _g("ts", "")
        finally:
            c.close()
    except Exception:
        return {"category": "UNKNOWN", "confidence": 0.0, "ts": None}
    return {"category": cat, "confidence": conf, "ts": ts}


def get_gpu_latest() -> list[dict]:
    """Read latest hardware_health snapshot from sqlite."""
    db = ROOT / "data" / "liril_hardware_health.sqlite"
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            r = c.execute(
                "SELECT payload FROM snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            c.close()
    except Exception:
        return []
    if not r:
        return []
    try:
        snap = json.loads(r[0])
    except Exception:
        return []
    return snap.get("gpus") or []


def get_cpu_temp() -> float | None:
    db = ROOT / "data" / "liril_hardware_health.sqlite"
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            r = c.execute(
                "SELECT payload FROM snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            c.close()
    except Exception:
        return None
    if not r:
        return None
    try:
        return json.loads(r[0]).get("cpu_c")
    except Exception:
        return None


def get_subject_rates() -> dict[str, int]:
    """Count events in observer DB per subject over last 5 min."""
    db = ROOT / "data" / "liril_observer.sqlite"
    if not db.exists():
        return {}
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            rows = c.execute(
                "SELECT subject, COUNT(*) FROM events "
                "WHERE ts >= ? GROUP BY subject ORDER BY 2 DESC",
                (time.time() - 300,),
            ).fetchall()
        finally:
            c.close()
    except Exception:
        return {}
    return {subj: int(cnt) for subj, cnt in rows}


def get_recent_incidents(n: int = 5) -> list[dict]:
    db = ROOT / "data" / "liril_failsafe.sqlite"
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(str(db), timeout=2)
        try:
            rows = c.execute(
                "SELECT id, ts, severity, source, message, acked "
                "FROM incidents ORDER BY ts DESC LIMIT ?",
                (max(1, int(n)),),
            ).fetchall()
        finally:
            c.close()
    except Exception:
        return []
    out = []
    for iid, ts, sev, src, msg, acked in rows:
        out.append({
            "id": iid[:8], "ts": ts, "age": int(time.time() - ts),
            "severity": sev, "source": src,
            "message": (msg or "")[:120], "acked": bool(acked),
        })
    return out


def get_recent_file_alerts(n: int = 3) -> list[dict]:
    """Parse the file_awareness jsonl audit log tail."""
    f = ROOT / "data" / "liril_file_audit.jsonl"
    if not f.exists():
        return []
    alerts: list[dict] = []
    try:
        # Tail the file — we only need last N alerts, so read last ~128 KB
        with f.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 128 * 1024))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        for line in lines[-200:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("kind") == "alert":
                    alerts.append({
                        "ts":    d.get("ts"),
                        "risk":  d.get("risk"),
                        "path":  d.get("path"),
                        "reasons": d.get("reasons") or [],
                    })
            except Exception:
                continue
    except Exception:
        pass
    return alerts[-max(1, int(n)):]


# ─────────────────────────────────────────────────────────────────────
# SUMMARY + RENDER
# ─────────────────────────────────────────────────────────────────────

def collect() -> dict:
    daemons = get_daemon_status()
    alive = sum(1 for d in daemons if d["alive"])
    dead  = [d["name"] for d in daemons if not d["alive"]]
    fse = get_fse_level()
    intent = get_intent()
    gpus = get_gpu_latest()
    cpu_c = get_cpu_temp()
    rates = get_subject_rates()
    incidents = get_recent_incidents(5)
    file_alerts = get_recent_file_alerts(3)

    if alive == len(DAEMONS) and fse["level"] < 3:
        overall = "RUNNING"
    elif alive >= len(DAEMONS) - 2 and fse["level"] < 3:
        overall = "DEGRADED"
    elif fse["level"] >= 4:
        overall = "SAFE_MODE"
    elif fse["level"] >= 3:
        overall = "RESTRICTED"
    else:
        overall = "DOWN"

    return {
        "ts":             datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "overall":        overall,
        "daemons_alive":  alive,
        "daemons_total":  len(DAEMONS),
        "daemons_dead":   dead,
        "daemons":        daemons,
        "fse":            fse,
        "intent":         intent,
        "gpus":           gpus,
        "cpu_c":          cpu_c,
        "subject_rates":  rates,
        "incidents":      incidents,
        "file_alerts":    file_alerts,
    }


def render_brief(s: dict) -> str:
    overall = s["overall"]
    color = {"RUNNING": GREEN, "DEGRADED": YELLOW, "RESTRICTED": RED,
             "SAFE_MODE": RED, "DOWN": RED}.get(overall, YELLOW)
    fse = s["fse"]
    intent = s["intent"]
    gpu_max = 0.0
    for g in s["gpus"]:
        t = g.get("temp_c") or 0
        if t > gpu_max:
            gpu_max = t
    parts = [
        color(f"LIRIL: {overall}"),
        f"{s['daemons_alive']}/{s['daemons_total']} daemons",
        f"fse={fse['level']}({fse['level_name']})",
        f"intent={intent.get('category','?')}",
        f"gpu_max={gpu_max:.0f}°C" if gpu_max else "gpu=?",
    ]
    return "  ·  ".join(parts)


def render_full(s: dict) -> str:
    out: list[str] = []
    W = 72
    def hr(char="─"):
        return DIM(char * W)

    # Header banner
    overall = s["overall"]
    color = {"RUNNING": GREEN, "DEGRADED": YELLOW, "RESTRICTED": RED,
             "SAFE_MODE": RED, "DOWN": RED}.get(overall, YELLOW)
    out.append(BOLD(color(f"  LIRIL  {overall}  ")) +
               DIM(f"   {s['ts']}"))
    out.append(hr())

    # Daemons
    alive = s["daemons_alive"]; total = s["daemons_total"]
    daemon_color = GREEN if alive == total else (YELLOW if alive >= total - 2 else RED)
    out.append(f"  Daemons: {daemon_color(str(alive) + '/' + str(total))} alive")
    if s["daemons_dead"]:
        out.append(f"    dead: {RED(', '.join(s['daemons_dead']))}")
    else:
        out.append(f"    {DIM('all daemons alive')}")

    # FSE
    fse = s["fse"]
    lvl_color = {"nominal": GREEN, "elevated": YELLOW, "alarmed": YELLOW,
                 "restricted": RED, "safe_mode": RED}.get(fse["level_name"], YELLOW)
    fse_lvl = fse["level"]
    fse_name = fse["level_name"]
    fse_inc = fse["incidents_10min"]
    fse_unack = fse["unacked_10min"]
    out.append(f"  FSE:     {lvl_color(f'level {fse_lvl} ({fse_name})')}   "
               f"{DIM(f'incidents/10min: {fse_inc} ({fse_unack} unacked)')}")
    if fse["reason"]:
        out.append(f"    {DIM('reason: ' + fse['reason'][:80])}")

    # Intent
    intent = s["intent"]
    intent_cat = intent.get("category", "?")
    intent_conf = intent.get("confidence", 0)
    out.append(f"  Intent:  {CYAN(intent_cat)}  "
               f"{DIM(f'(conf {intent_conf:.2f})')}")

    # Hardware
    gpu_lines = []
    for g in s["gpus"]:
        idx = g.get("index", 0)
        name = (g.get("name") or "")[:24]
        t = g.get("temp_c") or 0
        mem_u = g.get("mem_used_mb") or 0
        mem_t = g.get("mem_total_mb") or 1
        util = g.get("util_pct") or 0
        tc = GREEN if t < 80 else (YELLOW if t < 90 else RED)
        temp_str = tc(f"{t:.0f}°C")
        gpu_lines.append(
            f"    GPU{idx}  {name:24s}  "
            f"{temp_str}  util={util:.0f}%  "
            f"mem={mem_u/1024:.1f}/{mem_t/1024:.1f}GB"
        )
    out.append(f"  GPU:")
    out.extend(gpu_lines or [f"    {DIM('no data')}"])
    cpu = s["cpu_c"]
    if cpu is not None:
        cpu_str = GREEN(f"{cpu:.1f}°C") if cpu < 80 else YELLOW(f"{cpu:.1f}°C")
        out.append(f"  CPU:     {cpu_str}")

    # NATS rates
    out.append(hr("·"))
    out.append(f"  NATS last-5-min (top 8):")
    rate_items = list(s["subject_rates"].items())[:8]
    for subj, cnt in rate_items:
        out.append(f"    {CYAN(f'{cnt:>5}')}  {subj}")
    if not rate_items:
        out.append(f"    {DIM('no events in last 5 min (observer may not be running)')}")

    # Recent incidents
    if s["incidents"]:
        out.append(hr("·"))
        out.append(f"  Recent incidents (newest first):")
        for inc in s["incidents"]:
            age = inc["age"]
            sev = inc["severity"]
            sev_c = RED if sev == "critical" else (YELLOW if sev == "high" else DIM)
            ack = DIM("ACK") if inc["acked"] else "   "
            sev_str = sev_c(f"{sev:8s}")
            src_str = DIM(f"{inc['source'][:16]:16s}")
            out.append(f"    {ack} -{age:>5}s  {sev_str}  "
                       f"{src_str}  {inc['message'][:60]}")

    # File alerts
    if s["file_alerts"]:
        out.append(hr("·"))
        out.append(f"  Recent file alerts:")
        for fa in s["file_alerts"]:
            risk = fa.get("risk", "?")
            risk_c = RED if risk == "critical" else (YELLOW if risk == "high" else DIM)
            risk_str = risk_c(f"{risk:8s}")
            path_str = fa.get("path", "?")[:50]
            reason_str = DIM(", ".join(fa.get("reasons", []))[:40])
            out.append(f"    {risk_str}  {path_str:50s}  {reason_str}")

    out.append(hr())
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Status — one-command health view")
    ap.add_argument("--json",   action="store_true", help="JSON output")
    ap.add_argument("--brief",  action="store_true", help="Single-line summary")
    ap.add_argument("--watch",  action="store_true", help="Refresh every 5s")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Watch interval in seconds (default 5)")
    args = ap.parse_args()

    def one_shot() -> int:
        s = collect()
        if args.json:
            print(json.dumps(s, indent=2, default=str))
        elif args.brief:
            print(render_brief(s))
        else:
            print(render_full(s))
        # Exit 0 if RUNNING, 1 if DEGRADED, 2 if RESTRICTED/SAFE/DOWN
        return {"RUNNING": 0, "DEGRADED": 1}.get(s["overall"], 2)

    if not args.watch:
        return one_shot()

    try:
        while True:
            # Clear screen (best-effort)
            if sys.stdout.isatty():
                sys.stdout.write("\x1b[2J\x1b[H")
            one_shot()
            time.sleep(max(1.0, float(args.interval)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
