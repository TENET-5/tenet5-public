#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T03:50:00Z | Author: claude_code | Change: LIRIL runtime profiler (gap #1 partial)
"""LIRIL Runtime Profiler — passive statistics on dev-team role calls.

Starts to close LIRIL's perception gap #1:
  'Dynamic Code Analysis: observes changes in code logic, function calls,
   and data flow at runtime.'

LIRIL asked for this specifically:
  'B) Dynamic code tracer (gap #1). Focus on the researcher role and
   track function entries, argument lengths, and time spent for the top
   10 most-called functions. Include a summary of this data in the daily
   dev-team report at :8091.'

Design choice: we don't monkey-patch live daemons. The dev-team cycle
already records latency_ms + text length + role name + schema_ok for
every role invocation in each transcript at
E:/TENET-5.github.io/data/liril_dev_team_log/*.json. That's the call
record. This tool mines the transcripts into a structured profile
(sqlite-backed) that the :8091 dashboard can render, and the failure
analyzer can query.

NATS subjects:
  tenet5.liril.profile.roles    — request-reply, per-role stats
  tenet5.liril.profile.summary  — request-reply, top-line numbers
  tenet5.liril.profile.slowest  — request-reply, slowest cycles

CLI:
  python tools/liril_runtime_profiler.py --summary
  python tools/liril_runtime_profiler.py --role researcher
  python tools/liril_runtime_profiler.py --hourly 24
  python tools/liril_runtime_profiler.py --daemon
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
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TRANSCRIPTS = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")
PROFILE_DB  = Path(r"E:/S.L.A.T.E/tenet5/data/liril_runtime_profile.sqlite")
NATS_URL    = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ──────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    PROFILE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PROFILE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS role_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript  TEXT NOT NULL,
            task_id     TEXT,
            axis        TEXT,
            priority    TEXT,
            role        TEXT NOT NULL,
            latency_ms  INTEGER,
            text_chars  INTEGER,
            schema_ok   INTEGER,
            retried     INTEGER,
            had_error   INTEGER,
            had_loom    INTEGER,
            had_url_halt INTEGER,
            status      TEXT,
            verdict     TEXT,
            started_at  INTEGER,
            UNIQUE(transcript, role)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_role ON role_calls(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_axis ON role_calls(axis)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_started ON role_calls(started_at)")
    return conn


# ──────────────────────────────────────────────────────────────
# INGEST
# ──────────────────────────────────────────────────────────────

def ingest(limit: int | None = None, full: bool = False) -> dict:
    """Scan transcripts and upsert role_calls rows.

    If --full, re-ingest everything. Otherwise skip transcripts
    already in the DB.
    """
    if not TRANSCRIPTS.exists():
        return {"error": f"no transcript dir: {TRANSCRIPTS}"}

    conn = _db()
    if full:
        conn.execute("DELETE FROM role_calls")
        conn.commit()

    already = set()
    if not full:
        for row in conn.execute("SELECT DISTINCT transcript FROM role_calls"):
            already.add(row[0])

    files = sorted(
        TRANSCRIPTS.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if limit:
        files = files[:limit]

    n_new = n_skipped = n_bad = 0
    for f in files:
        name = f.name
        if name in already:
            n_skipped += 1
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            n_bad += 1
            continue

        task = d.get("task") or {}
        task_id = task.get("id", "?")
        axis = (task.get("axis_domain") or "").upper() or None
        priority = task.get("priority") or None
        status = d.get("status") or None
        verdict = d.get("verdict") or None
        mtime = int(f.stat().st_mtime)

        roles = d.get("roles") or []
        for r in roles:
            role = r.get("role") or "?"
            lat = r.get("latency_ms")
            text = r.get("text") or ""
            schema_ok = r.get("schema_ok")
            retried = r.get("retried")
            err = r.get("error")

            had_error = 1 if err else 0
            had_loom = 1 if "LOOM has" in text and "Fraction-exact" in text else 0
            # URL halt is recorded at cycle level not role level, but we
            # flag engineer role when task status is hallucination_caught
            had_url_halt = 1 if (role == "engineer" and status == "hallucination_caught") else 0

            try:
                conn.execute("""
                    INSERT OR REPLACE INTO role_calls
                    (transcript, task_id, axis, priority, role, latency_ms,
                     text_chars, schema_ok, retried, had_error, had_loom,
                     had_url_halt, status, verdict, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    name, task_id, axis, priority, role,
                    int(lat) if isinstance(lat, (int, float)) else None,
                    len(text),
                    1 if schema_ok else (0 if schema_ok is False else None),
                    1 if retried else 0,
                    had_error, had_loom, had_url_halt,
                    status, verdict, mtime,
                ))
            except sqlite3.IntegrityError:
                pass
        n_new += 1

    conn.commit()
    conn.close()
    return {"ingested": n_new, "skipped_already": n_skipped, "bad": n_bad,
            "total_files_seen": len(files)}


# ──────────────────────────────────────────────────────────────
# QUERIES
# ──────────────────────────────────────────────────────────────

def per_role_stats(hours: int | None = None) -> dict:
    """Per-role: call count, mean/median/p90 latency, mean chars, retry rate, error rate."""
    conn = _db()
    q = "SELECT role, latency_ms, text_chars, schema_ok, retried, had_error, had_loom, had_url_halt FROM role_calls"
    args: list = []
    if hours:
        cutoff = int(time.time() - hours * 3600)
        q += " WHERE started_at >= ?"
        args.append(cutoff)
    rows = conn.execute(q, args).fetchall()
    conn.close()

    by_role: dict = {}
    for role, lat, chars, ok, retried, err, loom, urlh in rows:
        b = by_role.setdefault(role, {"lats": [], "chars": [], "ok": 0, "n": 0,
                                       "retried": 0, "errors": 0, "loom": 0, "urlh": 0})
        b["n"] += 1
        if lat is not None:
            b["lats"].append(lat)
        b["chars"].append(chars or 0)
        if ok == 1: b["ok"] += 1
        b["retried"] += (retried or 0)
        b["errors"]  += (err or 0)
        b["loom"]    += (loom or 0)
        b["urlh"]    += (urlh or 0)

    out = {}
    for role, b in by_role.items():
        lats = b["lats"] or [0]
        chars = b["chars"] or [0]
        out[role] = {
            "calls":       b["n"],
            "mean_ms":     int(statistics.mean(lats)),
            "median_ms":   int(statistics.median(lats)),
            "p90_ms":      int(sorted(lats)[int(len(lats) * 0.9)] if len(lats) >= 10 else max(lats)),
            "max_ms":      int(max(lats)),
            "mean_chars":  int(statistics.mean(chars)),
            "max_chars":   int(max(chars)),
            "schema_ok_%": round(100 * b["ok"] / b["n"], 1) if b["n"] else 0.0,
            "retried_%":   round(100 * b["retried"] / b["n"], 1) if b["n"] else 0.0,
            "errors_%":    round(100 * b["errors"]  / b["n"], 1) if b["n"] else 0.0,
            "loom_pollution_count": b["loom"],
            "url_halt_count":       b["urlh"],
        }
    return out


def slowest_cycles(limit: int = 10) -> list[dict]:
    conn = _db()
    rows = conn.execute("""
        SELECT transcript, task_id, role, latency_ms, text_chars, status, verdict
        FROM role_calls
        WHERE latency_ms IS NOT NULL
        ORDER BY latency_ms DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [{"transcript": r[0], "task": r[1], "role": r[2], "ms": r[3],
             "chars": r[4], "status": r[5], "verdict": r[6]} for r in rows]


def hourly_throughput(hours: int = 24) -> list[dict]:
    """Commit/PASS rate per hour for the last N hours."""
    conn = _db()
    cutoff = int(time.time() - hours * 3600)
    rows = conn.execute("""
        SELECT (started_at / 3600) * 3600 AS hour, verdict, COUNT(*)
        FROM role_calls WHERE started_at >= ? AND role = 'gatekeeper'
        GROUP BY hour, verdict ORDER BY hour
    """, (cutoff,)).fetchall()
    conn.close()
    by_hour: dict = {}
    for h, v, n in rows:
        bucket = by_hour.setdefault(int(h), {"PASS": 0, "FAIL": 0, "WATCH": 0, "UNPARSED": 0, "other": 0})
        bucket[v if v in bucket else "other"] += n
    out = []
    for h, b in sorted(by_hour.items()):
        total = sum(b.values())
        out.append({
            "hour":       datetime.fromtimestamp(h, tz=timezone.utc).strftime("%H:%M"),
            "ts":         h,
            "total":      total,
            "pass":       b["PASS"],
            "pass_rate":  round(b["PASS"] / total, 3) if total else 0,
            "breakdown":  b,
        })
    return out


def summary() -> dict:
    conn = _db()
    total = conn.execute("SELECT COUNT(*) FROM role_calls").fetchone()[0]
    transcripts = conn.execute("SELECT COUNT(DISTINCT transcript) FROM role_calls").fetchone()[0]
    roles = dict(conn.execute(
        "SELECT role, COUNT(*) FROM role_calls GROUP BY role"
    ).fetchall())
    axes = dict(conn.execute(
        "SELECT axis, COUNT(*) FROM role_calls WHERE axis IS NOT NULL GROUP BY axis"
    ).fetchall())
    last_ts = conn.execute("SELECT MAX(started_at) FROM role_calls").fetchone()[0]
    pass_by_axis = {}
    for axis, total_n in axes.items():
        good = conn.execute("""
            SELECT COUNT(*) FROM role_calls
            WHERE axis = ? AND role = 'gatekeeper' AND verdict = 'PASS'
        """, (axis,)).fetchone()[0]
        # count of cycles for this axis
        cycles = conn.execute("""
            SELECT COUNT(DISTINCT transcript) FROM role_calls WHERE axis = ?
        """, (axis,)).fetchone()[0]
        pass_by_axis[axis] = {
            "cycles":    cycles,
            "pass_rate": round(good / cycles, 3) if cycles else 0,
        }
    conn.close()
    return {
        "calls":          total,
        "transcripts":    transcripts,
        "roles":          roles,
        "axes":           axes,
        "pass_by_axis":   pass_by_axis,
        "last_call_ts":   last_ts,
        "last_call_iso":  datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat() if last_ts else None,
    }


# ──────────────────────────────────────────────────────────────
# NATS
# ──────────────────────────────────────────────────────────────

async def daemon(interval: int = 60) -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[PROFILE] subscribed tenet5.liril.profile.* on {NATS_URL} (reingest every {interval}s)")

    # Seed initial ingestion
    ingest()

    async def h_roles(msg):
        try:
            req = json.loads(msg.data.decode() or "{}")
        except Exception:
            req = {}
        hours = req.get("hours")
        r = per_role_stats(hours=hours)
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_summary(msg):
        r = summary()
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_slowest(msg):
        try:
            req = json.loads(msg.data.decode() or "{}")
        except Exception:
            req = {}
        limit = int(req.get("limit", 10))
        r = slowest_cycles(limit)
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_hourly(msg):
        try:
            req = json.loads(msg.data.decode() or "{}")
        except Exception:
            req = {}
        hours = int(req.get("hours", 24))
        r = hourly_throughput(hours)
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    # Subscribe to git commits so we reingest after every new commit
    async def h_git_commit(msg):
        ingest(limit=20)  # cheap incremental

    await nc.subscribe("tenet5.liril.profile.roles",   cb=h_roles)
    await nc.subscribe("tenet5.liril.profile.summary", cb=h_summary)
    await nc.subscribe("tenet5.liril.profile.slowest", cb=h_slowest)
    await nc.subscribe("tenet5.liril.profile.hourly",  cb=h_hourly)
    await nc.subscribe("tenet5.liril.git.commit",      cb=h_git_commit)

    while True:
        try:
            ingest(limit=50)
        except Exception as e:
            print(f"[PROFILE] ingest error: {e!r}")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL runtime profiler")
    ap.add_argument("--ingest",  action="store_true", help="ingest all transcripts")
    ap.add_argument("--full",    action="store_true", help="with --ingest: re-ingest everything")
    ap.add_argument("--summary", action="store_true", help="overall stats")
    ap.add_argument("--role",    type=str, default=None, help="per-role stats (all roles if omitted)")
    ap.add_argument("--hours",   type=int, default=None, help="limit stats to last N hours")
    ap.add_argument("--slowest", type=int, nargs="?", const=10, help="N slowest role calls")
    ap.add_argument("--hourly",  type=int, nargs="?", const=24, help="throughput last N hours")
    ap.add_argument("--daemon",  action="store_true", help="ingest loop + NATS subjects")
    ap.add_argument("--json",    action="store_true")
    args = ap.parse_args()

    if args.daemon:
        asyncio.run(daemon())
        return 0

    if args.ingest:
        r = ingest(full=args.full)
        print(json.dumps(r, indent=2)) if args.json else print("ingest:", r)

    if args.summary:
        r = summary()
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"── profile summary ──")
            for k, v in r.items():
                if isinstance(v, dict):
                    print(f"  {k}:")
                    for k2, v2 in v.items(): print(f"    {k2}: {v2}")
                else:
                    print(f"  {k}: {v}")

    if args.role or (args.summary and not args.role):
        r = per_role_stats(hours=args.hours)
        if args.role:
            r = {args.role: r.get(args.role, {})}
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"\n── per-role stats {'(last '+str(args.hours)+'h)' if args.hours else ''} ──")
            for role, stats in r.items():
                print(f"\n  [{role}]")
                for k, v in stats.items(): print(f"    {k:20s}: {v}")

    if args.slowest is not None:
        r = slowest_cycles(args.slowest)
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"\n── slowest {args.slowest} role calls ──")
            for row in r:
                print(f"  {row['ms']:7d}ms  {row['role']:12s}  {row['task']:10s}  "
                      f"{row['status'] or '-':25s}  ({row['chars']} chars)")

    if args.hourly is not None:
        r = hourly_throughput(args.hourly)
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"\n── hourly throughput last {args.hourly}h ──")
            for row in r:
                pct = int(row['pass_rate'] * 100)
                print(f"  {row['hour']}  {row['total']:3d} cycles  PASS {row['pass']:2d} ({pct:3d}%)")

    if not (args.ingest or args.summary or args.role or args.slowest is not None or args.hourly is not None or args.daemon):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
