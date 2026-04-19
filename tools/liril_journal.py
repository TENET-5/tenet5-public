#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T14:30:00Z | Author: claude_code | Change: LIRIL Skill — Persistent-Memory Journal (remember/recall/search + RPC + TTL)
"""LIRIL Journal — persistent long-term memory.

LIRIL picked this as her next skill (2026-04-19):
"Prioritizes foundational learning infrastructure for future capabilities."

Why this exists
---------------
Each LIRIL capability already has its own sqlite cache (patch cache,
process allowlist, hardware snapshots, etc.) but those are cap-specific
and short-horizon. The journal is a CROSS-CAP, LONG-HORIZON memory
where any capability can deposit facts it wants to remember past its
own daemon restart.

Use cases (future — this ship is the substrate):
  - Cap#6 self-repair: "did restarting Spooler ever actually work?"
  - Cap#9 intent:       "Daniel usually starts CODING around 9am."
  - Cap#5 patch:        "Daniel vetoed this KB last time."
  - Cap#10 fse:         "this kind of anomaly escalated to critical 3x."
  - Cap#8 health:       "GPU0 temp pattern: idles at 45°C, peaks 72°C."

Data model
----------
Each entry: (id, key, value_json, tags_csv, source_cap, ts, ttl_sec, pinned)
  - key: short string, unique-ish (not strictly unique so multiple
         samples under the same key are OK)
  - value: arbitrary JSON
  - tags: comma-separated strings. Conventions:
      pref:*        user preferences (pinned by default, TTL=0)
      pattern:*     learned patterns
      incident:*    historical incidents
      decision:*    decisions Daniel made
      observation:* bulk telemetry samples
  - source_cap: which capability wrote this (for audit)
  - ttl_sec: 0 = forever; otherwise delete after this many seconds
  - pinned: 1 = never delete, even if over size cap

Retention + size cap
--------------------
  - Observation tag + TTL default 24h
  - Incident tag + TTL default 90d
  - Preference tag + TTL default 0 (forever, auto-pinned)
  - Soft cap: 100k rows. When exceeded, delete oldest non-pinned rows.
  - Hourly vacuum in daemon mode.

NATS surface
------------
  tenet5.liril.journal.write   (RPC) — remember() over NATS
  tenet5.liril.journal.read    (RPC) — recall() / search()
  tenet5.liril.journal.audit   — every write published for observer
  tenet5.liril.journal.metrics — 60-s snapshot (row count, by tag)

Python API (call from any cap via `from tools import liril_journal`)
-------------------------------------------------------------------
  remember(key, value, tags=None, source=None, ttl_sec=None, pinned=None) -> id
  recall(key=None, tag=None, limit=50, since_ts=None)                      -> list[row]
  search(text, limit=20)                                                    -> list[row]
  forget(entry_id)                                                          -> bool
  stats()                                                                   -> dict

CLI
---
  --remember KEY VALUE [--tags t1,t2] [--ttl SECONDS]
  --recall KEY
  --recall-tag TAG [--limit N]
  --search TEXT
  --forget ID
  --stats
  --export PATH
  --daemon
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
from datetime import datetime, timezone
from pathlib import Path

NATS_URL         = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
WRITE_SUBJECT    = "tenet5.liril.journal.write"
READ_SUBJECT     = "tenet5.liril.journal.read"
AUDIT_SUBJECT    = "tenet5.liril.journal.audit"
METRICS_SUBJECT  = "tenet5.liril.journal.metrics"

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH  = DATA_DIR / "liril_journal.sqlite"

# Default TTLs by tag prefix (seconds; 0 = forever)
_DEFAULT_TTL = {
    "pref:":         0,
    "pattern:":      0,
    "decision:":     0,
    "incident:":     90 * 86400,
    "observation:":  24 * 3600,
}

SOFT_ROW_CAP = 100_000
VACUUM_INTERVAL_SEC = 3600.0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    # Grok-review fix round 2 (2026-04-19): WAL mode + reasonable
    # busy_timeout. Without this, 17 writers will trip "database is
    # locked" under load + the LIKE fallback search path can starve.
    # WAL is file-persistent — once set, all future opens use it too.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id         TEXT PRIMARY KEY,
            key        TEXT NOT NULL,
            value_json TEXT NOT NULL,
            tags_csv   TEXT NOT NULL DEFAULT '',
            source_cap TEXT NOT NULL DEFAULT '',
            ts         REAL NOT NULL,
            ttl_sec    INTEGER NOT NULL DEFAULT 0,
            pinned     INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_entries_key   ON entries(key)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entries_ts    ON entries(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entries_tags  ON entries(tags_csv)")
    # FTS table for text search (if available — SQLite has FTS5 built in)
    try:
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
            USING fts5(id UNINDEXED, key, value_json, tags_csv,
                       content='entries', content_rowid='rowid')
        """)
    except Exception:
        # FTS5 might not be available on some Python builds — search falls
        # back to LIKE queries below.
        pass
    c.commit()
    return c


def _normalise_tags(tags) -> str:
    if tags is None:
        return ""
    if isinstance(tags, str):
        return ",".join(s.strip() for s in tags.split(",") if s.strip())
    if isinstance(tags, (list, tuple, set)):
        return ",".join(str(t).strip() for t in tags if str(t).strip())
    return ""


def _default_ttl_for(tags_csv: str) -> int:
    for t in (tags_csv or "").split(","):
        for prefix, ttl in _DEFAULT_TTL.items():
            if t.startswith(prefix):
                return ttl
    # No recognised tag prefix — default to observation TTL
    return _DEFAULT_TTL["observation:"]


def _default_pinned_for(tags_csv: str) -> int:
    # Preferences and decisions auto-pin
    for t in (tags_csv or "").split(","):
        if t.startswith("pref:") or t.startswith("decision:"):
            return 1
    return 0


# ─────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────

def remember(
    key: str,
    value,
    tags=None,
    source: str | None = None,
    ttl_sec: int | None = None,
    pinned: bool | None = None,
) -> str:
    """Store a fact. Returns the entry id."""
    if not key or not isinstance(key, str):
        raise ValueError("key must be a non-empty string")
    tags_csv = _normalise_tags(tags)
    ttl = int(ttl_sec) if ttl_sec is not None else _default_ttl_for(tags_csv)
    is_pinned = (1 if pinned else 0) if pinned is not None else _default_pinned_for(tags_csv)
    value_json = json.dumps(value, default=str)
    entry_id = str(uuid.uuid4())
    c = _db()
    try:
        c.execute(
            "INSERT OR REPLACE INTO entries"
            "(id, key, value_json, tags_csv, source_cap, ts, ttl_sec, pinned) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (entry_id, key[:256], value_json, tags_csv,
             (source or "")[:64], time.time(), ttl, is_pinned),
        )
        # FTS mirror (best-effort)
        try:
            c.execute(
                "INSERT OR REPLACE INTO entries_fts(id, key, value_json, tags_csv) "
                "VALUES(?,?,?,?)",
                (entry_id, key[:256], value_json, tags_csv),
            )
        except Exception:
            pass
        c.commit()
    finally:
        c.close()
    return entry_id


def recall(key: str | None = None, tag: str | None = None,
           limit: int = 50, since_ts: float | None = None) -> list[dict]:
    """Retrieve entries by key prefix or tag. Returns list of row dicts."""
    c = _db()
    try:
        _expire(c)
        clauses: list[str] = []
        params: list = []
        if key:
            clauses.append("key = ?")
            params.append(key)
        if tag:
            clauses.append("(',' || tags_csv || ',') LIKE ?")
            params.append(f"%,{tag},%")
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(float(since_ts))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (f"SELECT id, key, value_json, tags_csv, source_cap, ts, ttl_sec, pinned "
             f"FROM entries {where} ORDER BY ts DESC LIMIT ?")
        params.append(int(limit))
        rows = c.execute(q, params).fetchall()
    finally:
        c.close()
    return [_row_to_dict(r) for r in rows]


def search(text: str, limit: int = 20) -> list[dict]:
    """Fuzzy text search across key + value_json + tags. Prefers FTS5."""
    if not text or not text.strip():
        return []
    c = _db()
    try:
        _expire(c)
        # Try FTS first
        try:
            q = text.replace('"', '""')
            rows = c.execute(
                "SELECT e.id, e.key, e.value_json, e.tags_csv, e.source_cap, "
                "       e.ts, e.ttl_sec, e.pinned "
                "FROM entries_fts fts "
                "JOIN entries e ON e.id = fts.id "
                "WHERE entries_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (q, int(limit)),
            ).fetchall()
            if rows:
                return [_row_to_dict(r) for r in rows]
        except Exception:
            pass
        # Fallback: LIKE
        needle = f"%{text}%"
        rows = c.execute(
            "SELECT id, key, value_json, tags_csv, source_cap, ts, ttl_sec, pinned "
            "FROM entries "
            "WHERE key LIKE ? OR value_json LIKE ? OR tags_csv LIKE ? "
            "ORDER BY ts DESC LIMIT ?",
            (needle, needle, needle, int(limit)),
        ).fetchall()
    finally:
        c.close()
    return [_row_to_dict(r) for r in rows]


def forget(entry_id: str) -> bool:
    if not entry_id:
        return False
    c = _db()
    try:
        cur = c.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        try:
            c.execute("DELETE FROM entries_fts WHERE id = ?", (entry_id,))
        except Exception:
            pass
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def stats() -> dict:
    c = _db()
    try:
        _expire(c)
        total = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] or 0
        pinned = c.execute("SELECT COUNT(*) FROM entries WHERE pinned=1").fetchone()[0] or 0
        by_tag_raw = c.execute(
            "SELECT tags_csv, COUNT(*) FROM entries GROUP BY tags_csv"
        ).fetchall()
        by_prefix: dict[str, int] = {}
        for tags_csv, cnt in by_tag_raw:
            for t in (tags_csv or "").split(","):
                if not t:
                    continue
                # Use tag prefix (before ":") for grouping
                if ":" in t:
                    prefix = t.split(":", 1)[0] + ":"
                else:
                    prefix = t
                by_prefix[prefix] = by_prefix.get(prefix, 0) + int(cnt)
        oldest = c.execute("SELECT MIN(ts) FROM entries").fetchone()[0]
        newest = c.execute("SELECT MAX(ts) FROM entries").fetchone()[0]
    finally:
        c.close()
    return {
        "total":     int(total),
        "pinned":    int(pinned),
        "by_prefix": dict(sorted(by_prefix.items(), key=lambda x: -x[1])),
        "oldest_ts": oldest,
        "newest_ts": newest,
        "db_path":   str(DB_PATH),
    }


# ─────────────────────────────────────────────────────────────────────
# RETENTION
# ─────────────────────────────────────────────────────────────────────

def _expire(c: sqlite3.Connection) -> int:
    """Delete rows whose (ts + ttl_sec) < now, unless pinned. Returns count."""
    now = time.time()
    cur = c.execute(
        "DELETE FROM entries WHERE pinned=0 AND ttl_sec > 0 "
        "AND (ts + ttl_sec) < ?",
        (now,),
    )
    c.commit()
    return cur.rowcount


def _enforce_cap(c: sqlite3.Connection) -> int:
    """If row count > SOFT_ROW_CAP, drop oldest non-pinned rows. Returns deleted."""
    total = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] or 0
    if total <= SOFT_ROW_CAP:
        return 0
    excess = int(total) - SOFT_ROW_CAP
    # Delete oldest non-pinned
    c.execute(
        "DELETE FROM entries WHERE id IN ("
        "  SELECT id FROM entries WHERE pinned=0 ORDER BY ts ASC LIMIT ?"
        ")",
        (excess,),
    )
    c.commit()
    return excess


def vacuum() -> dict:
    c = _db()
    try:
        expired = _expire(c)
        dropped = _enforce_cap(c)
        # Also rebuild FTS index if present
        try:
            c.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
            c.commit()
        except Exception:
            pass
        return {"expired": expired, "dropped": dropped}
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────
# RPC + DAEMON
# ─────────────────────────────────────────────────────────────────────

def _row_to_dict(r) -> dict:
    try:
        val = json.loads(r[2])
    except Exception:
        val = r[2]
    return {
        "id":         r[0],
        "key":        r[1],
        "value":      val,
        "tags":       [t for t in (r[3] or "").split(",") if t],
        "source_cap": r[4],
        "ts":         r[5],
        "ttl_sec":    r[6],
        "pinned":     bool(r[7]),
    }


async def _on_write(msg, nc) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception:
        if msg.reply:
            try:
                await nc.publish(msg.reply,
                    json.dumps({"ok": False, "error": "bad json"}).encode())
            except Exception: pass
        return
    try:
        entry_id = remember(
            key=d.get("key", ""),
            value=d.get("value"),
            tags=d.get("tags"),
            source=d.get("source"),
            ttl_sec=d.get("ttl_sec"),
            pinned=d.get("pinned"),
        )
        response = {"ok": True, "id": entry_id}
        # Publish audit
        try:
            await nc.publish(AUDIT_SUBJECT, json.dumps({
                "ts":     _utc(),
                "kind":   "write",
                "id":     entry_id,
                "key":    d.get("key", ""),
                "tags":   d.get("tags"),
                "source": d.get("source"),
            }, default=str).encode())
        except Exception: pass
    except Exception as e:
        response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if msg.reply:
        try:
            await nc.publish(msg.reply, json.dumps(response, default=str).encode())
        except Exception: pass


async def _on_read(msg, nc) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception:
        if msg.reply:
            try:
                await nc.publish(msg.reply,
                    json.dumps({"ok": False, "error": "bad json"}).encode())
            except Exception: pass
        return
    op = (d.get("op") or "recall").lower()
    try:
        if op == "recall":
            rows = recall(
                key=d.get("key"),
                tag=d.get("tag"),
                limit=int(d.get("limit", 50)),
                since_ts=d.get("since_ts"),
            )
        elif op == "search":
            rows = search(text=d.get("text", ""),
                          limit=int(d.get("limit", 20)))
        elif op == "stats":
            rows = stats()
        else:
            rows = {"error": f"unknown op {op!r}"}
        response = {"ok": True, "result": rows}
    except Exception as e:
        response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if msg.reply:
        try:
            payload = json.dumps(response, default=str).encode()[:512 * 1024]
            await nc.publish(msg.reply, payload)
        except Exception: pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[JOURNAL] daemon starting — RPC on {WRITE_SUBJECT} + {READ_SUBJECT}")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[JOURNAL] NATS unavailable: {e!r}")
        return

    # nats-py requires cb to be `async def`, so wrap the module-level helpers
    async def _write_cb(msg):
        await _on_write(msg, nc)
    async def _read_cb(msg):
        await _on_read(msg, nc)
    await nc.subscribe(WRITE_SUBJECT, cb=_write_cb)
    await nc.subscribe(READ_SUBJECT,  cb=_read_cb)

    last_metric = 0.0
    last_vacuum = 0.0
    try:
        while True:
            now = time.time()
            # Periodic vacuum + metrics
            if now - last_vacuum >= VACUUM_INTERVAL_SEC:
                try:
                    r = vacuum()
                    if r["expired"] or r["dropped"]:
                        print(f"[JOURNAL] vacuum: expired={r['expired']} dropped={r['dropped']}")
                except Exception as e:
                    print(f"[JOURNAL] vacuum error: {type(e).__name__}: {e}")
                last_vacuum = now
            if now - last_metric >= 60.0:
                try:
                    snap = stats()
                    await nc.publish(METRICS_SUBJECT,
                                     json.dumps(snap, default=str).encode())
                except Exception:
                    pass
                last_metric = now
            await asyncio.sleep(5.0)
    finally:
        try: await nc.drain()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Journal — persistent memory")
    ap.add_argument("--remember",   nargs=2, metavar=("KEY", "VALUE"),
                    help='Store a fact. VALUE is parsed as JSON (or kept as string).')
    ap.add_argument("--tags",       type=str, default="",
                    help="Comma-separated tags (e.g. pref:notifications,desktop)")
    ap.add_argument("--source",     type=str, default="cli")
    ap.add_argument("--ttl",        type=int, default=None,
                    help="Override TTL seconds (0 = forever)")
    ap.add_argument("--pin",        action="store_true", help="Pin the entry")
    ap.add_argument("--recall",     type=str, metavar="KEY",
                    help="Recall entries with exact key match")
    ap.add_argument("--recall-tag", type=str, metavar="TAG",
                    help="Recall all entries carrying TAG")
    ap.add_argument("--limit",      type=int, default=50)
    ap.add_argument("--search",     type=str, metavar="TEXT")
    ap.add_argument("--forget",     type=str, metavar="ID")
    ap.add_argument("--stats",      action="store_true")
    ap.add_argument("--vacuum",     action="store_true")
    ap.add_argument("--export",     type=str, metavar="PATH",
                    help="Write all entries to JSON at PATH")
    ap.add_argument("--daemon",     action="store_true")
    args = ap.parse_args()

    if args.remember:
        key, raw = args.remember
        try:
            value = json.loads(raw)
        except Exception:
            value = raw   # keep as string
        entry_id = remember(
            key=key, value=value,
            tags=args.tags or None,
            source=args.source or None,
            ttl_sec=args.ttl,
            pinned=(True if args.pin else None),
        )
        print(json.dumps({"ok": True, "id": entry_id}, indent=2))
        return 0

    if args.recall:
        rows = recall(key=args.recall, limit=args.limit)
        print(json.dumps(rows, indent=2, default=str)[:8000])
        return 0

    if args.recall_tag:
        rows = recall(tag=args.recall_tag, limit=args.limit)
        print(json.dumps(rows, indent=2, default=str)[:8000])
        return 0

    if args.search:
        rows = search(args.search, limit=args.limit)
        print(json.dumps(rows, indent=2, default=str)[:8000])
        return 0

    if args.forget:
        ok = forget(args.forget)
        print(json.dumps({"ok": ok, "id": args.forget}))
        return 0 if ok else 1

    if args.stats:
        print(json.dumps(stats(), indent=2, default=str))
        return 0

    if args.vacuum:
        print(json.dumps(vacuum(), indent=2))
        return 0

    if args.export:
        c = _db()
        try:
            rows = c.execute(
                "SELECT id, key, value_json, tags_csv, source_cap, ts, ttl_sec, pinned "
                "FROM entries ORDER BY ts ASC"
            ).fetchall()
        finally:
            c.close()
        out = [_row_to_dict(r) for r in rows]
        Path(args.export).write_text(
            json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(f"exported {len(out)} entries to {args.export}")
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[JOURNAL] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
