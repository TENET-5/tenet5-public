#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T13:00:00Z | Author: claude_code | Change: LIRIL Skill — User Communication Channels (toast + TTS + intent-aware suppression)
"""LIRIL User Communication — LIRIL's user-facing notification layer.

LIRIL picked this at severity=med when asked what new skills she wanted:
"Facilitates direct user interaction through voice and notifications."

What it does
------------
Any capability can publish to `tenet5.liril.communication.notify` with a
severity + message. This daemon:
  1. Pulls the notification off NATS.
  2. Checks Cap#9 user-intent: if user is GAMING or MEETING, defers high-severity
     items to a queue, drops low-severity items entirely.
  3. Renders via the best available channel:
        Critical → TTS + desktop toast (can't be missed)
        High     → desktop toast
        Medium   → desktop toast (auto-dismiss)
        Low      → silent; logged to sqlite only
  4. Throttles duplicate messages (same hash within 5 min → dropped).

Channels
--------
  DESKTOP TOAST — PowerShell BurntToast if available, else msg.exe
  TTS           — System.Speech.Synthesis.SpeechSynthesizer (Windows SAPI)
  LOG ONLY      — sqlite, audit jsonl

All three are local — no external dependencies, no admin.

Intent-aware suppression
------------------------
  IDLE    → route normally (user will see on return)
  GAMING  → defer high; drop medium/low
  MEETING → defer high; drop medium/low
  CODING  → route normally, but TTS off for medium (don't interrupt flow)
  WRITING → same as CODING
  else    → route normally

Deferred items are kept in sqlite and re-attempted every 60 s; on intent
transition away from GAMING/MEETING, the queue is drained immediately.

CLI
---
  --notify SEVERITY "TITLE" "BODY"   Send a one-shot notification
  --speak "TEXT"                     Speak (respects intent suppression)
  --test                             Toast + TTS test
  --drain                            Force-drain deferred queue
  --daemon                           Run the NATS subscriber
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NATS_URL        = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
NOTIFY_SUBJECT  = "tenet5.liril.communication.notify"
SPEAK_SUBJECT   = "tenet5.liril.communication.speak"
AUDIT_SUBJECT   = "tenet5.liril.communication.audit"
METRICS_SUBJECT = "tenet5.liril.communication.metrics"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DB   = DATA_DIR / "liril_communication.sqlite"
AUDIT_LOG  = DATA_DIR / "liril_communication_audit.jsonl"

DEDUP_WINDOW_SEC = 300.0   # 5 min
DRAIN_INTERVAL   = 60.0


# ─────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(STATE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dedup (
            hash     TEXT PRIMARY KEY,
            last_ts  REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS deferred (
            hash       TEXT PRIMARY KEY,
            ts         REAL NOT NULL,
            severity   TEXT NOT NULL,
            title      TEXT,
            body       TEXT,
            reason     TEXT
        )
    """)
    c.commit()
    return c


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _dedup_check(h: str) -> bool:
    """Return True if message should be sent (not a duplicate)."""
    c = _db()
    try:
        now = time.time()
        r = c.execute("SELECT last_ts FROM dedup WHERE hash=?", (h,)).fetchone()
        if r and now - float(r[0]) < DEDUP_WINDOW_SEC:
            return False
        c.execute("INSERT OR REPLACE INTO dedup(hash, last_ts) VALUES(?, ?)",
                  (h, now))
        # Trim old entries
        c.execute("DELETE FROM dedup WHERE last_ts < ?",
                  (now - DEDUP_WINDOW_SEC * 2,))
        c.commit()
        return True
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────
# INTENT LOOKUP (via Cap#9 public API)
# ─────────────────────────────────────────────────────────────────────

def _current_intent() -> str:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_user_intent as _intent  # type: ignore
        return (_intent.current_intent().get("category") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _route(severity: str, intent: str) -> tuple[str, str]:
    """Return (action, reason). Action ∈ {send, defer, drop}."""
    sev = (severity or "").lower()
    i   = (intent or "").upper()
    if i in ("GAMING", "MEETING"):
        if sev == "critical":
            return "send", "critical overrides intent suppression"
        if sev == "high":
            return "defer", f"deferred until intent != {i}"
        return "drop", f"intent={i}, severity={sev}"
    if i in ("CODING", "WRITING") and sev in ("medium", "med", "low"):
        return "send_quiet", f"intent={i} — send without TTS"
    return "send", "normal"


# ─────────────────────────────────────────────────────────────────────
# RENDER CHANNELS
# ─────────────────────────────────────────────────────────────────────

def _render_toast(title: str, body: str) -> tuple[bool, str]:
    """Desktop toast. Prefers BurntToast (PS module), falls back to msg.exe."""
    safe_title = (title or "LIRIL").replace("'", "''")[:80]
    safe_body  = (body or "").replace("'", "''")[:400]
    # Try BurntToast
    ps = (
        "if (Get-Module -ListAvailable -Name BurntToast) {"
        "  Import-Module BurntToast -ErrorAction SilentlyContinue;"
        f"  New-BurntToastNotification -Text '{safe_title}','{safe_body}' -ErrorAction Stop;"
        "  'burnt'"
        "} else { 'no-burnt' }"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0 and "burnt" in (r.stdout or ""):
            return True, "burnttoast"
    except Exception:
        pass
    # Fallback: Win32 msg.exe (blocks until dismissed; use /TIME to auto-close)
    try:
        r = subprocess.run(
            ["msg.exe", "*", "/TIME:10", f"{title}: {body}"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, "msg.exe"
    except Exception:
        pass
    return False, "no channel available"


def _render_tts(text: str) -> tuple[bool, str]:
    """Speak via Windows SAPI — System.Speech.Synthesis.SpeechSynthesizer."""
    safe_text = (text or "").replace("'", "''")[:500]
    ps = (
        "try {"
        "  Add-Type -AssemblyName System.Speech -ErrorAction Stop;"
        "  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "  $s.Rate = 0;"
        f"  $s.Speak('{safe_text}');"
        "  'spoken'"
        "} catch { 'tts-failed: ' + $_.Exception.Message }"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        if "spoken" in out:
            return True, "sapi"
        return False, out[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _render_log_only(title: str, body: str, severity: str) -> tuple[bool, str]:
    _audit({
        "kind":     "log_only",
        "ts":       _utc(),
        "title":    title,
        "body":     body,
        "severity": severity,
    })
    return True, "log_only"


# ─────────────────────────────────────────────────────────────────────
# DISPATCH
# ─────────────────────────────────────────────────────────────────────

def dispatch_notify(severity: str, title: str, body: str,
                    defer_on_busy: bool = True) -> dict:
    """Core dispatch: dedupe → intent route → render."""
    sev = (severity or "low").lower()
    title = (title or "LIRIL")[:120]
    body  = (body or "")[:1000]
    h = hashlib.sha1(f"{sev}|{title}|{body}".encode("utf-8")).hexdigest()[:16]

    if not _dedup_check(h):
        return {"ok": True, "action": "dedup_suppressed", "hash": h}

    intent = _current_intent()
    action, reason = _route(sev, intent)

    result = {
        "ts":       _utc(),
        "hash":     h,
        "severity": sev,
        "title":    title,
        "intent":   intent,
        "action":   action,
        "reason":   reason,
    }

    if action == "drop":
        _audit({"kind": "dropped", **result})
        return {**result, "ok": True, "channel": None}

    if action == "defer" and defer_on_busy:
        c = _db()
        try:
            c.execute(
                "INSERT OR REPLACE INTO deferred(hash, ts, severity, title, body, reason) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (h, time.time(), sev, title, body, reason),
            )
            c.commit()
        finally:
            c.close()
        _audit({"kind": "deferred", **result})
        return {**result, "ok": True, "channel": "deferred"}

    # Render
    channels_used: list[str] = []
    ok_overall = False

    if sev == "critical":
        # TTS + toast
        t_ok, t_how = _render_tts(f"Critical: {title}. {body}")
        if t_ok:
            channels_used.append(f"tts:{t_how}")
            ok_overall = True
        n_ok, n_how = _render_toast(title, body)
        if n_ok:
            channels_used.append(f"toast:{n_how}")
            ok_overall = True
    elif sev in ("high", "medium", "med"):
        # Toast; optional TTS (suppressed if action == send_quiet)
        n_ok, n_how = _render_toast(title, body)
        if n_ok:
            channels_used.append(f"toast:{n_how}")
            ok_overall = True
        if sev == "high" and action != "send_quiet":
            t_ok, t_how = _render_tts(title)
            if t_ok:
                channels_used.append(f"tts:{t_how}")
    else:  # low
        lg_ok, lg_how = _render_log_only(title, body, sev)
        if lg_ok:
            channels_used.append(f"log:{lg_how}")
            ok_overall = True

    result["ok"] = ok_overall
    result["channels"] = channels_used
    _audit({"kind": "sent" if ok_overall else "send_failed", **result})
    return result


def drain_deferred() -> int:
    """Re-attempt all deferred items. Returns number sent."""
    c = _db()
    try:
        rows = c.execute(
            "SELECT hash, severity, title, body FROM deferred ORDER BY ts ASC"
        ).fetchall()
    finally:
        c.close()
    sent = 0
    for h, sev, title, body in rows:
        intent = _current_intent()
        if intent in ("GAMING", "MEETING") and sev != "critical":
            continue
        r = dispatch_notify(sev, title or "LIRIL", body or "", defer_on_busy=False)
        if r.get("ok"):
            c = _db()
            try:
                c.execute("DELETE FROM deferred WHERE hash=?", (h,))
                c.commit()
            finally:
                c.close()
            sent += 1
    return sent


# ─────────────────────────────────────────────────────────────────────
# DAEMON
# ─────────────────────────────────────────────────────────────────────

async def _on_notify(msg) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception:
        return
    sev = d.get("severity") or d.get("sev") or "low"
    title = d.get("title") or d.get("subject") or ""
    body  = d.get("body")  or d.get("message") or ""
    try:
        dispatch_notify(sev, title, body)
    except Exception as e:
        _audit({"kind": "dispatch_exception", "ts": _utc(),
                "error": f"{type(e).__name__}: {e}"})


async def _on_speak(msg) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception:
        return
    text = d.get("text") or d.get("body") or ""
    if not text:
        return
    intent = _current_intent()
    if intent in ("GAMING", "MEETING"):
        _audit({"kind": "speak_suppressed", "ts": _utc(),
                "intent": intent, "text": text[:200]})
        return
    _render_tts(text)


async def _daemon() -> None:
    import nats as _nats
    print("[COMM] daemon starting")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[COMM] NATS unavailable: {e!r}")
        return
    await nc.subscribe(NOTIFY_SUBJECT, cb=_on_notify)
    await nc.subscribe(SPEAK_SUBJECT,  cb=_on_speak)

    last_drain = 0.0
    last_metric = 0.0
    try:
        while True:
            # Drain deferred queue
            if time.time() - last_drain >= DRAIN_INTERVAL:
                try:
                    n = drain_deferred()
                    if n:
                        print(f"[COMM] drained {n} deferred notifications")
                except Exception as e:
                    print(f"[COMM] drain error: {type(e).__name__}: {e}")
                last_drain = time.time()
            # Metrics
            if time.time() - last_metric >= 60:
                try:
                    c = _db()
                    try:
                        n_def = c.execute("SELECT COUNT(*) FROM deferred").fetchone()[0]
                    finally:
                        c.close()
                    snap = {
                        "ts":       _utc(),
                        "deferred": n_def,
                        "intent":   _current_intent(),
                    }
                    await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
                except Exception:
                    pass
                last_metric = time.time()
            await asyncio.sleep(5)
    finally:
        try: await nc.drain()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL User Communication")
    ap.add_argument("--notify",  nargs=3, metavar=("SEVERITY", "TITLE", "BODY"),
                    help="Send a notification")
    ap.add_argument("--speak",   type=str, metavar="TEXT", help="Speak TEXT via TTS")
    ap.add_argument("--test",    action="store_true", help="Fire a test toast + TTS")
    ap.add_argument("--drain",   action="store_true", help="Drain deferred queue now")
    ap.add_argument("--daemon",  action="store_true", help="Run NATS subscriber loop")
    args = ap.parse_args()

    if args.notify:
        sev, title, body = args.notify
        r = dispatch_notify(sev, title, body)
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("ok") else 1

    if args.speak:
        intent = _current_intent()
        if intent in ("GAMING", "MEETING"):
            print(f"suppressed (intent={intent})")
            return 0
        ok, how = _render_tts(args.speak)
        print(f"{'spoken' if ok else 'FAILED'} via {how}")
        return 0 if ok else 1

    if args.test:
        r1 = dispatch_notify("high", "LIRIL test",
                             "This is a LIRIL communication channel test.")
        print("toast:", r1.get("action"), r1.get("channels"))
        intent = _current_intent()
        if intent not in ("GAMING", "MEETING"):
            ok, how = _render_tts("LIRIL communication test complete.")
            print(f"tts: {how} ok={ok}")
        else:
            print(f"tts: suppressed (intent={intent})")
        return 0

    if args.drain:
        n = drain_deferred()
        print(f"drained {n}")
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[COMM] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
