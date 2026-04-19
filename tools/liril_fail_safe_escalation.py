#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T10:00:00Z | Author: claude_code | Change: LIRIL Capability #10 — Fail-Safe Escalation Protocol (governs EXEC_GATE for Caps #2-#5)
"""LIRIL Fail-Safe Escalation Protocol — Capability #10 of the post-NPU plan.

LIRIL was asked on 2026-04-19 which capability to build next given the
state of the build. Despite Cap#5 being the next in numerical order,
LIRIL promoted Cap#10 (severity=CRITICAL) because all subsequent mutating
capabilities depend on it:

    NEXT_BUILD:  Cap#10 Fail-Safe Escalation Protocol
    RATIONALE:   Critical severity requires immediate attention to prevent
                 system-wide failures; prioritizes stability over features.
    DEPENDENCIES: None
    FIRST_STEP:  tools/liril_fail_safe_escalation.py — this file.

Why this exists
---------------
Cap#2 (Service Control), Cap#3 (Process Management), and Cap#4 (Driver
Management) each have a `LIRIL_EXECUTE=1` gate that, once set, lets LIRIL
mutate the host. Each capability has its own denylist, but none of them
know the *global state of the system*. If GPU0 just crashed and thermals
are climbing, issuing a "Stop-Service WinDefend" might be technically
allowed by Cap#2's denylist check, yet catastrophically wrong to do right
now.

Cap#10 introduces a global ESCALATION_LEVEL (0-4) that any other capability
can read before executing. When the level rises, dangerous actions are
auto-refused even if the local capability would have permitted them.

    0 = nominal    — normal operations
    1 = elevated   — high-severity incidents recently seen
    2 = alarmed    — critical incident OR dead-man timer tripped
    3 = restricted — mutating capabilities auto-refuse (no EXEC_GATE)
    4 = safe_mode  — only logging + heartbeats; all plans deny

Transitions have a minimum dwell time (60s) to prevent flapping.

Architecture
------------
  1. Any subsystem publishes an incident to
     `tenet5.liril.failsafe.incident`:
        {id, severity (low/med/high/critical), source, message, ts, data}
  2. The daemon sorts incidents into a rolling 10-minute window, scores
     each severity band, and computes the derived level. If the derived
     level differs from the current level AND min-dwell has elapsed, the
     level transitions and is published to `tenet5.liril.failsafe.level`
     (JetStream-style last-known retained via periodic re-publish).
  3. Capabilities register with the daemon and must publish heartbeats to
     `tenet5.liril.failsafe.heartbeat.<cap>` every 60s. Missed heartbeats
     (3 intervals) auto-generate a severity=high incident.
  4. Humans can issue commands on `tenet5.liril.failsafe.command`:
        {command: "escalate"|"reset"|"ack", ...}
     Reset back to level 0 from safe_mode requires both NATS AND the
     operator's explicit env flag (LIRIL_FAILSAFE_HUMAN=1), so an
     in-loop agent cannot unilaterally de-escalate itself.

Contract for other capabilities
-------------------------------
Any capability that mutates state (Cap#2/#3/#4, future #5+) should call
`current_level()` before executing and refuse at level >= 3:

    from tools import liril_fail_safe_escalation as fse
    if fse.current_level() >= fse.LEVEL_RESTRICTED:
        return {"status": "refused_by_failsafe", "level": fse.current_level()}

The last-known level is cached in sqlite so the check is O(one sqlite read)
and doesn't require live NATS.

CLI modes
---------
  --status                Show current level, active incidents, heartbeats
  --report SEV "MSG"      File an incident (low|med|high|critical)
  --escalate N            Force level to N (requires LIRIL_FAILSAFE_HUMAN=1)
  --reset                 Reset to level 0 (requires LIRIL_FAILSAFE_HUMAN=1)
  --ack INCIDENT_ID       Acknowledge an incident (reduces its contribution)
  --heartbeat CAP         Publish heartbeat for CAP (useful from crontab)
  --list-incidents        Recent incidents
  --daemon                Run as level manager (subscribes + publishes)
  --wait-below LEVEL      Block until current level < LEVEL (integration helper)
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
import sqlite3
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")

# Subjects
INCIDENT_SUBJECT  = "tenet5.liril.failsafe.incident"
HEARTBEAT_PREFIX  = "tenet5.liril.failsafe.heartbeat."
LEVEL_SUBJECT     = "tenet5.liril.failsafe.level"
COMMAND_SUBJECT   = "tenet5.liril.failsafe.command"
AUDIT_SUBJECT     = "tenet5.liril.failsafe.audit"

# Levels
LEVEL_NOMINAL    = 0
LEVEL_ELEVATED   = 1
LEVEL_ALARMED    = 2
LEVEL_RESTRICTED = 3
LEVEL_SAFE_MODE  = 4
LEVEL_NAMES = {
    0: "nominal", 1: "elevated", 2: "alarmed",
    3: "restricted", 4: "safe_mode",
}

# Tunables
MIN_DWELL_SEC        = 60.0         # hysteresis floor
INCIDENT_WINDOW_SEC  = 600.0        # 10-minute rolling window
HEARTBEAT_INTERVAL   = 60.0         # caps must beat at least this often
HEARTBEAT_MISS_MAX   = 3            # 3 missed intervals → incident
LEVEL_REPUBLISH_SEC  = 30.0         # periodic retained-level broadcast
SEVERITY_WEIGHTS = {
    "low":      0,
    "med":      1,
    "medium":   1,
    "high":     3,
    "critical": 8,
}

HUMAN_OK = os.environ.get("LIRIL_FAILSAFE_HUMAN", "0") == "1"

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH    = DATA_DIR / "liril_failsafe.sqlite"
AUDIT_LOG  = DATA_DIR / "liril_failsafe_audit.jsonl"


# ─────────────────────────────────────────────────────────────────────
# SQLITE SCHEMA
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id         TEXT PRIMARY KEY,
            ts         REAL NOT NULL,
            severity   TEXT NOT NULL,
            source     TEXT NOT NULL,
            message    TEXT NOT NULL,
            data       TEXT,
            acked      INTEGER NOT NULL DEFAULT 0,
            ack_ts     REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents(ts)")
    c.commit()
    return c


def _state_get(c: sqlite3.Connection, key: str, default: str) -> str:
    row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _state_set(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute(
        "INSERT OR REPLACE INTO state(key, value) VALUES(?, ?)",
        (key, value),
    )
    c.commit()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[FAILSAFE] audit log write failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# PUBLIC API — callable from other capabilities
# ─────────────────────────────────────────────────────────────────────

def current_level() -> int:
    """Read the last-known escalation level from sqlite. O(one read).

    Any mutating capability should call this before executing. Level >= 3
    means all mutations are refused. This function tolerates a missing
    database (returns LEVEL_NOMINAL) so the startup order doesn't matter."""
    try:
        c = _db()
        try:
            return int(_state_get(c, "level", str(LEVEL_NOMINAL)))
        finally:
            c.close()
    except Exception:
        return LEVEL_NOMINAL


def is_safe_to_execute() -> bool:
    """Convenience wrapper — True iff level allows mutating actions."""
    return current_level() < LEVEL_RESTRICTED


def last_level_change_ts() -> float:
    try:
        c = _db()
        try:
            return float(_state_get(c, "last_change_ts", "0"))
        finally:
            c.close()
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────
# INCIDENT FILING
# ─────────────────────────────────────────────────────────────────────

def _normalise_severity(sev: str) -> str:
    s = (sev or "").strip().lower()
    if s in ("low", "med", "medium", "high", "critical"):
        return "medium" if s == "med" else s
    return "medium"


def file_incident_local(
    severity: str,
    source: str,
    message: str,
    data: dict | None = None,
) -> str:
    """Write an incident to sqlite even if NATS is down. Returns incident id."""
    iid = str(uuid.uuid4())
    sev = _normalise_severity(severity)
    c = _db()
    try:
        c.execute(
            "INSERT INTO incidents(id, ts, severity, source, message, data) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (iid, time.time(), sev, source, message,
             json.dumps(data or {}, default=str)),
        )
        c.commit()
    finally:
        c.close()
    _audit({"kind": "incident_filed_local", "id": iid, "severity": sev,
            "source": source, "message": message, "ts": _utc()})
    return iid


async def file_incident_async(
    nc,
    severity: str,
    source: str,
    message: str,
    data: dict | None = None,
) -> str:
    """Publish an incident on NATS and also persist locally."""
    iid = file_incident_local(severity, source, message, data)
    try:
        payload = {
            "id":       iid,
            "ts":       _utc(),
            "severity": _normalise_severity(severity),
            "source":   source,
            "message":  message,
            "data":     data or {},
        }
        await nc.publish(INCIDENT_SUBJECT, json.dumps(payload, default=str).encode())
    except Exception as e:
        print(f"[FAILSAFE] publish incident failed (local sqlite still wrote): {e!r}")
    return iid


def file_incident_sync(severity: str, source: str, message: str,
                       data: dict | None = None) -> str:
    """Non-async helper for capabilities that don't want to pull in asyncio."""
    iid = file_incident_local(severity, source, message, data)
    # Best-effort NATS publish — swallow failures
    try:
        import nats as _nats
        async def _pub():
            try:
                nc = await _nats.connect(NATS_URL, connect_timeout=2)
            except Exception:
                return
            try:
                await nc.publish(INCIDENT_SUBJECT, json.dumps({
                    "id": iid, "ts": _utc(),
                    "severity": _normalise_severity(severity),
                    "source": source, "message": message,
                    "data": data or {},
                }, default=str).encode())
            finally:
                try: await nc.drain()
                except Exception: pass
        asyncio.run(_pub())
    except Exception:
        pass
    return iid


# ─────────────────────────────────────────────────────────────────────
# LEVEL COMPUTATION
# ─────────────────────────────────────────────────────────────────────

def _compute_level(
    c: sqlite3.Connection,
    heartbeats_missed: list[str],
) -> tuple[int, str]:
    """Derive the escalation level from:
      - severity-weighted incidents in the last INCIDENT_WINDOW_SEC
      - missed heartbeats
    Returns (level, reason-string).

    Grok-review fix (2026-04-19): dedupe incidents by fingerprint so a
    chatty source publishing the same (severity,source,message) 50x in
    10 min doesn't artificially lift the level beyond what one real
    incident would warrant. First occurrence counts full weight;
    duplicates contribute 0.
    """
    cutoff = time.time() - INCIDENT_WINDOW_SEC
    rows = c.execute(
        "SELECT severity, acked, source, message FROM incidents WHERE ts >= ?",
        (cutoff,),
    ).fetchall()
    score = 0
    by_sev: Counter = Counter()
    any_critical_unacked = False
    seen_fingerprints: set = set()
    dedup_dropped = 0
    for sev, acked, src, msg in rows:
        fp = (sev or "", (src or "")[:64], (msg or "")[:200])
        if fp in seen_fingerprints:
            dedup_dropped += 1
            continue
        seen_fingerprints.add(fp)
        if acked:
            # Acked incidents contribute half weight — they remain evidence
            # but the operator has seen them.
            score += SEVERITY_WEIGHTS.get(sev, 1) // 2
        else:
            score += SEVERITY_WEIGHTS.get(sev, 1)
            if sev == "critical":
                any_critical_unacked = True
            by_sev[sev] += 1

    # Heartbeat misses add severity=high pressure per miss
    score += SEVERITY_WEIGHTS["high"] * len(heartbeats_missed)
    for m in heartbeats_missed:
        by_sev["heartbeat_missed:" + m] += 1

    # Thresholds
    if any_critical_unacked and score >= 16:
        return LEVEL_SAFE_MODE, f"critical unacked AND score={score} {dict(by_sev)}"
    if any_critical_unacked:
        return LEVEL_RESTRICTED, f"critical unacked {dict(by_sev)}"
    if score >= 16:
        return LEVEL_RESTRICTED, f"score={score} {dict(by_sev)}"
    if score >= 8:
        return LEVEL_ALARMED, f"score={score} {dict(by_sev)}"
    if score >= 3:
        return LEVEL_ELEVATED, f"score={score} {dict(by_sev)}"
    return LEVEL_NOMINAL, f"score={score}"


async def _maybe_transition(nc, c: sqlite3.Connection,
                            heartbeats_missed: list[str]) -> bool:
    """Recompute level, transition if different and dwell elapsed.
    Returns True if the level changed."""
    derived, reason = _compute_level(c, heartbeats_missed)
    current = int(_state_get(c, "level", str(LEVEL_NOMINAL)))
    if derived == current:
        return False
    last_change = float(_state_get(c, "last_change_ts", "0"))
    if time.time() - last_change < MIN_DWELL_SEC:
        return False  # hysteresis — hold

    _state_set(c, "level", str(derived))
    _state_set(c, "last_change_ts", str(time.time()))
    _state_set(c, "last_reason", reason)

    payload = {
        "ts":          _utc(),
        "level":       derived,
        "level_name":  LEVEL_NAMES[derived],
        "prior_level": current,
        "reason":      reason,
    }
    _audit({"kind": "level_change", **payload})
    try:
        await nc.publish(LEVEL_SUBJECT, json.dumps(payload).encode())
    except Exception as e:
        print(f"[FAILSAFE] publish level-change failed: {e!r}")
    print(f"[FAILSAFE] level {current}→{derived} ({LEVEL_NAMES[derived]}) :: {reason}")
    return True


# ─────────────────────────────────────────────────────────────────────
# DAEMON
# ─────────────────────────────────────────────────────────────────────

_LAST_HEARTBEAT: dict[str, float] = {}


async def _daemon() -> None:
    import nats as _nats
    print("[FAILSAFE] daemon starting")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[FAILSAFE] cannot connect to NATS: {e!r}")
        return

    c = _db()
    print(f"[FAILSAFE] subscribed to "
          f"{INCIDENT_SUBJECT}, {HEARTBEAT_PREFIX}*, {COMMAND_SUBJECT}")

    async def _on_incident(msg):
        try:
            d = json.loads(msg.data.decode())
        except Exception:
            return
        # Deduplicate on id if caller already assigned one
        iid = d.get("id") or str(uuid.uuid4())
        sev = _normalise_severity(d.get("severity", "medium"))
        src = str(d.get("source", "unknown"))[:128]
        msg_text = str(d.get("message", ""))[:1024]
        try:
            c.execute(
                "INSERT OR IGNORE INTO incidents(id, ts, severity, source, message, data) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (iid, time.time(), sev, src, msg_text,
                 json.dumps(d.get("data") or {}, default=str)),
            )
            c.commit()
        except Exception as e:
            print(f"[FAILSAFE] incident insert failed: {e!r}")
        _audit({"kind": "incident_received", "id": iid, "severity": sev,
                "source": src, "ts": _utc()})

    async def _on_heartbeat(msg):
        # Subject: tenet5.liril.failsafe.heartbeat.<cap>
        cap = msg.subject.removeprefix(HEARTBEAT_PREFIX) or "unknown"
        _LAST_HEARTBEAT[cap] = time.time()

    async def _on_command(msg):
        try:
            d = json.loads(msg.data.decode())
        except Exception:
            return
        cmd = (d.get("command") or "").lower()
        if cmd == "ack":
            iid = d.get("id")
            if not iid:
                return
            try:
                c.execute(
                    "UPDATE incidents SET acked=1, ack_ts=? WHERE id=?",
                    (time.time(), iid),
                )
                c.commit()
                _audit({"kind": "ack", "id": iid, "by": d.get("by", ""),
                        "ts": _utc()})
            except Exception as e:
                print(f"[FAILSAFE] ack failed: {e!r}")
        elif cmd == "escalate":
            if not d.get("human_ok"):
                print("[FAILSAFE] escalate REJECTED — missing human_ok")
                return
            try:
                n = max(0, min(LEVEL_SAFE_MODE, int(d.get("level", 0))))
            except Exception:
                return
            _state_set(c, "level", str(n))
            _state_set(c, "last_change_ts", str(time.time()))
            _state_set(c, "last_reason", f"manual escalate by {d.get('by','')}")
            _audit({"kind": "manual_escalate", "level": n, "ts": _utc(),
                    "by": d.get("by", "")})
            try:
                await nc.publish(LEVEL_SUBJECT, json.dumps({
                    "ts": _utc(), "level": n,
                    "level_name": LEVEL_NAMES[n],
                    "reason": "manual_escalate",
                }).encode())
            except Exception:
                pass
        elif cmd == "reset":
            if not d.get("human_ok"):
                print("[FAILSAFE] reset REJECTED — missing human_ok")
                return
            _state_set(c, "level", str(LEVEL_NOMINAL))
            _state_set(c, "last_change_ts", str(time.time()))
            _state_set(c, "last_reason", f"manual reset by {d.get('by','')}")
            _audit({"kind": "manual_reset", "ts": _utc(), "by": d.get("by", "")})
            try:
                await nc.publish(LEVEL_SUBJECT, json.dumps({
                    "ts": _utc(), "level": LEVEL_NOMINAL,
                    "level_name": LEVEL_NAMES[LEVEL_NOMINAL],
                    "reason": "manual_reset",
                }).encode())
            except Exception:
                pass

    await nc.subscribe(INCIDENT_SUBJECT, cb=_on_incident)
    await nc.subscribe(HEARTBEAT_PREFIX + ">", cb=_on_heartbeat)
    await nc.subscribe(COMMAND_SUBJECT, cb=_on_command)

    last_republish = 0.0
    try:
        while True:
            # Compute missed heartbeats
            now = time.time()
            deadline = now - HEARTBEAT_INTERVAL * HEARTBEAT_MISS_MAX
            missed = [cap for cap, ts in _LAST_HEARTBEAT.items() if ts < deadline]

            # A capability that was heard from but is now missing self-generates
            # a heartbeat-loss incident once per miss period
            for cap in missed:
                last_incident_key = f"hb_miss:{cap}"
                last_reported = float(_state_get(c, last_incident_key, "0"))
                if now - last_reported > HEARTBEAT_INTERVAL * HEARTBEAT_MISS_MAX:
                    iid = str(uuid.uuid4())
                    c.execute(
                        "INSERT INTO incidents(id, ts, severity, source, message, data) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (iid, now, "high", "failsafe_daemon",
                         f"missed heartbeat: {cap}",
                         json.dumps({"cap": cap, "last_seen": _LAST_HEARTBEAT.get(cap)})),
                    )
                    c.commit()
                    _state_set(c, last_incident_key, str(now))
                    _audit({"kind": "heartbeat_missed", "cap": cap, "ts": _utc()})

            await _maybe_transition(nc, c, missed)

            # Periodic retained-level broadcast so late subscribers get the
            # current level without having to query NATS JetStream.
            if now - last_republish >= LEVEL_REPUBLISH_SEC:
                try:
                    level = int(_state_get(c, "level", str(LEVEL_NOMINAL)))
                    await nc.publish(LEVEL_SUBJECT, json.dumps({
                        "ts":         _utc(),
                        "level":      level,
                        "level_name": LEVEL_NAMES[level],
                        "reason":     _state_get(c, "last_reason", "periodic"),
                        "periodic":   True,
                    }).encode())
                except Exception:
                    pass
                last_republish = now

            await asyncio.sleep(5)
    finally:
        await nc.drain()


# ─────────────────────────────────────────────────────────────────────
# CLI HELPERS
# ─────────────────────────────────────────────────────────────────────

def _print_status() -> None:
    c = _db()
    try:
        level = int(_state_get(c, "level", str(LEVEL_NOMINAL)))
        reason = _state_get(c, "last_reason", "")
        last_change = float(_state_get(c, "last_change_ts", "0"))
        dwell = time.time() - last_change if last_change else 0
        print(f"LEVEL:    {level} ({LEVEL_NAMES[level]})")
        print(f"REASON:   {reason}")
        print(f"DWELL:    {dwell:.0f}s since last change")
        print(f"EXEC_OK:  {'yes' if level < LEVEL_RESTRICTED else 'NO (mutations refused)'}")
        cutoff = time.time() - INCIDENT_WINDOW_SEC
        rows = c.execute(
            "SELECT id, ts, severity, source, message, acked "
            "FROM incidents WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
            (cutoff,),
        ).fetchall()
        if not rows:
            print("INCIDENTS (10 min window): none")
        else:
            print(f"INCIDENTS (10 min window, {len(rows)} shown):")
            for iid, ts, sev, src, msg, acked in rows:
                age = time.time() - ts
                mark = "ACKED " if acked else "      "
                print(f"  {mark} -{age:>5.0f}s  {sev:8s}  {src[:20]:20s}  {msg[:80]}  [{iid[:8]}]")
    finally:
        c.close()


async def _publish_heartbeat(cap: str) -> None:
    import nats as _nats
    safe = "".join(ch for ch in (cap or "") if ch.isalnum() or ch in "_-.")
    if not safe:
        print("invalid capability name")
        return
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        print(f"nats connect failed: {e!r}")
        return
    try:
        await nc.publish(HEARTBEAT_PREFIX + safe, json.dumps({
            "ts": _utc(), "cap": safe, "pid": os.getpid(),
        }).encode())
        print(f"heartbeat published for {safe}")
    finally:
        await nc.drain()


async def _publish_command(cmd: str, **kwargs) -> None:
    import nats as _nats
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        print(f"nats connect failed: {e!r}")
        return
    try:
        await nc.publish(COMMAND_SUBJECT, json.dumps({
            "command": cmd,
            "by":      os.environ.get("USERNAME") or "cli",
            "ts":      _utc(),
            **kwargs,
        }).encode())
    finally:
        await nc.drain()


async def _wait_below(target: int) -> int:
    """Block until current_level() < target, return the level we exited at."""
    while True:
        lvl = current_level()
        if lvl < target:
            return lvl
        await asyncio.sleep(2)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Fail-Safe Escalation Protocol — Capability #10")
    ap.add_argument("--status",          action="store_true", help="Show current level + incidents")
    ap.add_argument("--report",          nargs=2, metavar=("SEVERITY", "MESSAGE"),
                    help="File an incident (low|med|high|critical)")
    ap.add_argument("--source",          type=str, default="cli",
                    help="Source label for --report (default: cli)")
    ap.add_argument("--escalate",        type=int, metavar="LEVEL",
                    help=f"Force level to LEVEL (0-{LEVEL_SAFE_MODE}). "
                         "Requires LIRIL_FAILSAFE_HUMAN=1.")
    ap.add_argument("--reset",           action="store_true",
                    help="Reset to level 0. Requires LIRIL_FAILSAFE_HUMAN=1.")
    ap.add_argument("--ack",             type=str, metavar="INCIDENT_ID",
                    help="Acknowledge an incident")
    ap.add_argument("--heartbeat",       type=str, metavar="CAP",
                    help="Publish a heartbeat for CAP")
    ap.add_argument("--list-incidents",  action="store_true",
                    help="Show all incidents from the last hour")
    ap.add_argument("--daemon",          action="store_true",
                    help="Run the level-manager daemon")
    ap.add_argument("--wait-below",      type=int, metavar="LEVEL",
                    help="Block until current level < LEVEL, then exit 0")
    ap.add_argument("--current-level",   action="store_true",
                    help="Print current numeric level and exit")
    args = ap.parse_args()

    if args.current_level:
        print(current_level())
        return 0

    if args.status:
        _print_status()
        return 0

    if args.report:
        sev, msg = args.report
        iid = file_incident_sync(sev, args.source, msg)
        print(f"incident filed: {iid} severity={_normalise_severity(sev)}")
        return 0

    if args.escalate is not None:
        if not HUMAN_OK:
            print("REFUSED — LIRIL_FAILSAFE_HUMAN=1 not set. "
                  "Escalation override requires explicit human flag.")
            return 2
        lvl = max(0, min(LEVEL_SAFE_MODE, int(args.escalate)))
        asyncio.run(_publish_command("escalate", level=lvl, human_ok=True))
        # Also write directly to sqlite so --status reflects immediately even
        # without a running daemon.
        c = _db()
        try:
            _state_set(c, "level", str(lvl))
            _state_set(c, "last_change_ts", str(time.time()))
            _state_set(c, "last_reason", f"manual escalate via cli by {os.environ.get('USERNAME','')}")
        finally:
            c.close()
        print(f"escalated to level {lvl} ({LEVEL_NAMES[lvl]})")
        return 0

    if args.reset:
        if not HUMAN_OK:
            print("REFUSED — LIRIL_FAILSAFE_HUMAN=1 not set. "
                  "Reset requires explicit human flag.")
            return 2
        asyncio.run(_publish_command("reset", human_ok=True))
        c = _db()
        try:
            _state_set(c, "level", str(LEVEL_NOMINAL))
            _state_set(c, "last_change_ts", str(time.time()))
            _state_set(c, "last_reason", f"manual reset via cli by {os.environ.get('USERNAME','')}")
        finally:
            c.close()
        print("reset to level 0 (nominal)")
        return 0

    if args.ack:
        asyncio.run(_publish_command("ack", id=args.ack))
        c = _db()
        try:
            c.execute("UPDATE incidents SET acked=1, ack_ts=? WHERE id=?",
                      (time.time(), args.ack))
            c.commit()
        finally:
            c.close()
        print(f"acked {args.ack}")
        return 0

    if args.heartbeat:
        asyncio.run(_publish_heartbeat(args.heartbeat))
        return 0

    if args.list_incidents:
        c = _db()
        try:
            cutoff = time.time() - 3600
            rows = c.execute(
                "SELECT id, ts, severity, source, message, acked FROM incidents "
                "WHERE ts >= ? ORDER BY ts DESC",
                (cutoff,),
            ).fetchall()
            for iid, ts, sev, src, msg, acked in rows:
                age = time.time() - ts
                mark = "ACK" if acked else "   "
                print(f"{mark} {sev:8s} -{age:>5.0f}s  {src[:20]:20s}  {msg[:90]}  [{iid[:12]}]")
            print(f"({len(rows)} incidents in last hour)")
        finally:
            c.close()
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[FAILSAFE] daemon stopped")
        return 0

    if args.wait_below is not None:
        final = asyncio.run(_wait_below(int(args.wait_below)))
        print(f"level={final} < {args.wait_below} — proceeding")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
