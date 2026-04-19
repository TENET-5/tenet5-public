#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T14:35:00Z | Author: claude_code | Change: subscribe to tenet5.liril.journal.* (persistent memory stream)
"""LIRIL Observer \u2014 unified NATS subscriber + aggregator.

LIRIL picked this as the next build (option A):
  'Ship the liril_observer daemon. This will provide a unified live
   stream of crucial data points, enabling better visibility and
   understanding of our system\\'s operations. To ensure maximum impact,
   I\\'ll include a real-time task completion rate metric, highlighting
   the effectiveness of our QAOA scheduler.'

Subscribes to every signal-bearing NATS subject LIRIL already publishes
and maintains a rolling window in sqlite at data/liril_observer.sqlite:

  tenet5.liril.scheduler.decisions  \u2014 QAOA picks
  tenet5.liril.git.commit           \u2014 commits (autonomous + manual)
  tenet5.liril.os.findings          \u2014 Windows / tenet5-linux scan
  tenet5.pvsnp.telemetry            \u2014 bench cycles
  tenet5.liril.decompose.report     \u2014 3-strikes auto-decomposer
  tenet5.liril.docs.hint            \u2014 git-watcher fired doc-rescan

Also computes derived metrics:
  - task completion rate (completed today / attempted today)
  - QAOA scheduler coverage (picks with score published / total picks)
  - commit velocity (last hour / last 6h / today)
  - auto vs manual commit ratio

Exposes:
  tenet5.liril.observer.recent  request-reply \u2014 last N events
  tenet5.liril.observer.metrics request-reply \u2014 computed metrics
  :8092/metrics                 JSON metrics snapshot
  :8092/stream                  SSE live feed

The dashboard (:8091) pulls via direct import + NATS request-reply
for its 'Live activity' panel.
"""
from __future__ import annotations

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
from collections import Counter, deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

OBSERVER_DB = Path(r"E:/S.L.A.T.E/tenet5/data/liril_observer.sqlite")
NATS_URL    = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
HTTP_PORT   = int(os.environ.get("LIRIL_OBSERVER_PORT", 8092))

# Subjects we watch
SUBJECTS = [
    "tenet5.liril.scheduler.decisions",
    "tenet5.liril.git.commit",
    "tenet5.liril.os.findings",
    "tenet5.pvsnp.telemetry",
    "tenet5.liril.decompose.report",
    "tenet5.liril.docs.hint",
    # 2026-04-19: LIRIL NPU-domain capability #1 — Windows System Monitor
    "windows.system.metrics",
    "tenet5.liril.os.alert",
    # 2026-04-19: LIRIL NPU-domain capability #2 — Windows Service Control
    "windows.service.control",
    # 2026-04-19: LIRIL NPU-domain capability #3 — Windows Process Management
    "windows.process.metrics",
    "windows.process.control",
    # 2026-04-19: LIRIL NPU-domain capability #4 — Windows Driver Management
    "windows.driver.metrics",
    "windows.driver.control",
    # 2026-04-19: LIRIL post-NPU capability #10 — Fail-Safe Escalation Protocol
    "tenet5.liril.failsafe.incident",
    "tenet5.liril.failsafe.level",
    "tenet5.liril.failsafe.audit",
    # 2026-04-19: LIRIL post-NPU capability #8 — Hardware Health Telemetry
    "windows.hardware.health.metrics",
    # 2026-04-19: LIRIL 24/7 supervisor
    "tenet5.liril.supervisor.status",
    # 2026-04-19: LIRIL NPU-domain capability #5 — Windows Patch Management
    "windows.patch.metrics",
    "windows.patch.control",
    # 2026-04-19: LIRIL post-NPU capability #6 — Autonomous Self-Repair
    "windows.repair.metrics",
    "windows.repair.control",
    # 2026-04-19: LIRIL post-NPU capability #9 — User-Intent Prediction
    "tenet5.liril.intent.current",
    "tenet5.liril.intent.transition",
    "tenet5.liril.intent.metrics",
    # 2026-04-19: LIRIL new skills (outbound network + communication + file awareness)
    "tenet5.liril.network.audit",
    "tenet5.liril.network.metrics",
    "tenet5.liril.communication.audit",
    "tenet5.liril.communication.metrics",
    "tenet5.liril.file.alert",
    "tenet5.liril.file.metrics",
    # 2026-04-19: LIRIL persistent-memory journal
    "tenet5.liril.journal.audit",
    "tenet5.liril.journal.metrics",
    # 2026-04-19: LIRIL autonomous cron (website + OSINT + TENET5 OS + Docker)
    "tenet5.liril.autonomous.metrics",
    "tenet5.liril.autonomous.audit",
    # 2026-04-19: Gastown Docker cron — 111³ SATOR grid dispatcher
    "tenet.111.metrics",
    "tenet.111.nats_health",
    "tenet.111.gastown",
    "tenet.111.convergence",
    # NOTE: tenet.111.heartbeat publishes every 100ms — too noisy to record;
    # observer is deliberately NOT subscribing to it to avoid sqlite bloat.
    # (Gastown itself counts heartbeats; we just need the derived metrics.)
    # 2026-04-19: LIRIL goal-decomposition engine
    "tenet5.liril.goals.audit",
    "tenet5.liril.goals.metrics",
]

# In-memory rolling window
RECENT: deque = deque(maxlen=500)
# Per-subject counts
SUB_COUNTS: Counter = Counter()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ──────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    OBSERVER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OBSERVER_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         INTEGER NOT NULL,
            subject    TEXT NOT NULL,
            summary    TEXT,
            body_bytes INTEGER,
            payload    TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts      ON events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subject ON events(subject)")
    return conn


# ──────────────────────────────────────────────────────────────
# SUMMARISER — extract a one-line summary per subject
# ──────────────────────────────────────────────────────────────

def _summarise(subject: str, payload: dict) -> str:
    if subject == "tenet5.liril.scheduler.decisions":
        picks = payload.get("picks") or []
        if picks:
            p = picks[0]
            return f"QAOA picked {p.get('task_id')} score={p.get('score')}"
        return f"QAOA decision ({payload.get('method')}, {payload.get('solve_ms')}ms)"
    if subject == "tenet5.liril.git.commit":
        sha = (payload.get("sha") or "")[:10]
        repo = payload.get("repo", "?")
        sub = payload.get("subject", "")[:70]
        mark = "auto" if payload.get("is_autonomous") else "manual"
        return f"[{mark}] {repo} {sha} {sub}"
    if subject == "tenet5.liril.os.findings":
        f = len((payload.get("windows") or {}).get("findings", []) or []) + \
            len((payload.get("tenet5") or {}).get("findings", []) or [])
        gaps = (payload.get("gap") or {}).get("total_gaps", 0)
        return f"OS scan: {f} findings, {gaps} Windows\u2192tenet5 gaps"
    if subject == "tenet5.pvsnp.telemetry":
        sc = payload.get("scaling") or {}
        exps = {p: s.get("exponent") for p, s in sc.items()}
        return f"bench cycle: scaling {exps}"
    if subject == "tenet5.liril.decompose.report":
        return (f"decomposer: candidates={payload.get('candidates',0)} "
                f"decomposed={payload.get('decomposed',0)} "
                f"subs={payload.get('sub_tasks_added',0)}")
    if subject == "tenet5.liril.docs.hint":
        files = payload.get("files") or []
        return f"docs.hint: {len(files)} files rescan"
    return json.dumps(payload)[:140]


# ──────────────────────────────────────────────────────────────
# METRICS
# ──────────────────────────────────────────────────────────────

def _metrics() -> dict:
    conn = _db()
    now = int(time.time())
    hr1 = now - 3600
    hr6 = now - 6 * 3600
    hr24 = now - 24 * 3600

    def _count(subject_like: str, since: int) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE subject LIKE ? AND ts >= ?",
            (subject_like, since),
        ).fetchone()[0]

    commits_auto_1h = conn.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE subject = 'tenet5.liril.git.commit' AND ts >= ? "
        "AND summary LIKE '[auto]%'",
        (hr1,),
    ).fetchone()[0]
    commits_manual_1h = conn.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE subject = 'tenet5.liril.git.commit' AND ts >= ? "
        "AND summary LIKE '[manual]%'",
        (hr1,),
    ).fetchone()[0]

    total_1h = _count("tenet5.liril.git.commit", hr1)
    total_6h = _count("tenet5.liril.git.commit", hr6)
    total_24h = _count("tenet5.liril.git.commit", hr24)
    qaoa_decisions_24h = _count("tenet5.liril.scheduler.decisions", hr24)

    conn.close()

    # Compute task-completion rate from recent dev-team transcripts
    # (we read them directly since the transcripts dir is the source of truth)
    transcripts_dir = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")
    cycles_24h_total = 0
    cycles_24h_pass = 0
    if transcripts_dir.exists():
        cutoff = now - 24 * 3600
        for f in transcripts_dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            cycles_24h_total += 1
            if d.get("verdict") == "PASS":
                cycles_24h_pass += 1
    pass_rate_24h = round(cycles_24h_pass / cycles_24h_total, 3) if cycles_24h_total else 0.0

    return {
        "ts":                      now,
        "sub_counts":              dict(SUB_COUNTS),
        "recent_events_memory":    len(RECENT),
        "commits_last_hour":       total_1h,
        "commits_last_6_hours":    total_6h,
        "commits_last_24_hours":   total_24h,
        "autonomous_last_hour":    commits_auto_1h,
        "manual_last_hour":        commits_manual_1h,
        "qaoa_decisions_last_24h": qaoa_decisions_24h,
        "cycles_last_24h":         cycles_24h_total,
        "cycles_passed_last_24h":  cycles_24h_pass,
        "pass_rate_last_24h":      pass_rate_24h,
    }


def recent(limit: int = 50) -> list[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT ts, subject, summary FROM events "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"ts": r[0],
             "iso": datetime.fromtimestamp(r[0], tz=timezone.utc).isoformat(),
             "subject": r[1],
             "summary": r[2]} for r in rows]


# ──────────────────────────────────────────────────────────────
# NATS SUBSCRIBER
# ──────────────────────────────────────────────────────────────

async def _ingest_event(subject: str, payload: dict, raw_bytes: int) -> None:
    SUB_COUNTS[subject] += 1
    summary = _summarise(subject, payload)
    ts = int(time.time())
    conn = _db()
    conn.execute(
        "INSERT INTO events (ts, subject, summary, body_bytes, payload) VALUES (?, ?, ?, ?, ?)",
        (ts, subject, summary, raw_bytes,
         json.dumps(payload, default=str)[:4000]),
    )
    conn.commit()
    conn.close()
    RECENT.appendleft({
        "ts": ts,
        "iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "subject": subject,
        "summary": summary,
    })
    # Optional: echo to stdout so daemon log shows activity
    print(f"[OBS] {subject:38s} {summary[:100]}")


async def _nats_main() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("[OBS] nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[OBS] connected {NATS_URL}; subscribing {len(SUBJECTS)} subjects")

    async def make_handler(subject: str):
        async def _cb(msg):
            try:
                payload = json.loads(msg.data.decode())
            except Exception:
                payload = {"raw": msg.data.decode(errors="replace")[:500]}
            await _ingest_event(subject, payload, len(msg.data))
        return _cb

    for sub in SUBJECTS:
        await nc.subscribe(sub, cb=await make_handler(sub))

    # Request-reply endpoints
    async def h_recent(msg):
        try:
            req = json.loads(msg.data.decode() or "{}")
        except Exception:
            req = {}
        limit = int(req.get("limit", 50))
        await nc.publish(msg.reply, json.dumps(recent(limit), default=str).encode())

    async def h_metrics(msg):
        await nc.publish(msg.reply, json.dumps(_metrics(), default=str).encode())

    await nc.subscribe("tenet5.liril.observer.recent",  cb=h_recent)
    await nc.subscribe("tenet5.liril.observer.metrics", cb=h_metrics)

    while True:
        await asyncio.sleep(60)


# ──────────────────────────────────────────────────────────────
# HTTP SERVER \u2014 :8092/metrics + /stream + /recent
# ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj: dict | list, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            self._json(_metrics())
            return
        if self.path.startswith("/recent"):
            limit = 50
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("limit="):
                        try:
                            limit = int(kv.split("=", 1)[1])
                        except Exception:
                            pass
            self._json(recent(limit))
            return
        if self.path == "/stream":
            # Server-Sent Events
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last_sent = -1
            try:
                while True:
                    while RECENT and RECENT[0]["ts"] > last_sent:
                        ev = RECENT[0]
                        last_sent = ev["ts"]
                        self.wfile.write(b"data: " +
                                         json.dumps(ev, default=str).encode() +
                                         b"\n\n")
                        self.wfile.flush()
                        # only emit the newest one; break if already past
                        break
                    time.sleep(1)
            except Exception:
                return
            return
        self.send_response(404)
        self.end_headers()


def _http_server() -> None:
    srv = HTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    print(f"[OBS] http :{HTTP_PORT}/metrics + /recent + /stream")
    srv.serve_forever()


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main() -> int:
    # Start HTTP in a thread
    Thread(target=_http_server, daemon=True).start()
    # Run NATS subscriber in the main event loop
    try:
        asyncio.run(_nats_main())
    except KeyboardInterrupt:
        print("[OBS] shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
