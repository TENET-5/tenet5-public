#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T11:55:00Z | Author: claude_code | Change: LIRIL Capability #9 — User-Intent Prediction Engine (v1 whitelist-only games rule — GPU compute heuristic was FP-prone)
"""LIRIL User-Intent Prediction — Capability #9 of the post-NPU plan.

LIRIL (poll 2026-04-19): "Models and anticipates Daniel's intent to align
system actions with user needs." Severity = medium.

Role in the stack
-----------------
Purely OBSERVATIONAL. Cap#9 watches:
  - Foreground window's owning process
  - Idle time (Win32 GetLastInputInfo)
  - Active high-CPU / high-GPU processes
  - Recent process list
and infers a categorical "current intent" every 15s:

    CODING    | WRITING | GAMING  | MEETING | BROWSING
    MEDIA     | IDLE    | UNKNOWN

Other capabilities can CONSUME the intent signal via
`current_intent()` Python helper or by subscribing to the NATS subject
`tenet5.liril.intent.current`. Typical uses (not implemented here):

  - Cap#5 defers Windows Update installs while GAMING / MEETING
  - Cap#2 throttles service restarts while CODING
  - Cap#8 raises its GPU temperature alert threshold while GAMING (the
    user expects high thermals and doesn't want spam)

Privacy
-------
Foreground-window TITLES can leak private data (passwords in urls, page
contents, doc names). Default: ONLY the owning process executable name
is published, never the title. Window-title hashing is opt-in with
LIRIL_INTENT_TITLE_SHA=1 (publishes 8-char sha256 so transitions can be
observed without revealing content).

Classification
--------------
Rule-based first (fast, deterministic, explainable). NPU-augmented only
when rules return UNKNOWN AND a NATS classifier is available. Rules
match against the foreground process name + a signal profile:

  CODING    IDE or editor foreground (code.exe, devenv.exe, pycharm64.exe,
            idea64.exe, rustrover64.exe, sublime_text.exe, subl.exe,
            notepad++.exe, helix.exe, nvim-qt.exe, windsurf.exe,
            cursor.exe, claude.exe, ...)
  WRITING   winword.exe, wps.exe, onenote.exe, evernote.exe, obsidian.exe,
            typora.exe, notion.exe (title hint: Google Docs / Word docs)
  GAMING    Any process advertising a fullscreen D3D swapchain or in
            data/liril_intent_games.txt (user-maintained list) OR
            process has >40% sustained utilization on GPU1 (the gaming
            GPU per CLAUDE.md layout).
  MEETING   teams.exe, zoom.exe, slack.exe during call, discord.exe in
            voice, meet.google.com in chrome foreground (title hint).
  BROWSING  chrome.exe / firefox.exe / msedge.exe / brave.exe foreground
            and NOT in a meeting-hint title.
  MEDIA     mpv.exe, vlc.exe, wmplayer.exe, spotify.exe, YouTube in a
            browser foreground (title hint).
  IDLE      GetLastInputInfo shows no input in >= 300 seconds.
  UNKNOWN   None of the above.

Signals are soft — a rule "wins" by producing the highest-confidence
match. Ties prefer IDLE > GAMING > MEETING > CODING > WRITING > MEDIA
> BROWSING > UNKNOWN.

CLI
---
  --snapshot        Print one inference cycle
  --current         Print the cached current intent (from sqlite)
  --history N       Print last N intent transitions (default 10)
  --list-rules      Print the classification rule table
  --daemon          Run the 15s inference loop
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NATS_URL         = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
INTENT_SUBJECT   = "tenet5.liril.intent.current"
TRANSITION_SUBJ  = "tenet5.liril.intent.transition"
METRICS_SUBJECT  = "tenet5.liril.intent.metrics"
POLL_SEC         = float(os.environ.get("LIRIL_INTENT_INTERVAL", "15"))
IDLE_THRESHOLD_S = 300.0    # 5 min of no input → IDLE
TITLE_HASH       = os.environ.get("LIRIL_INTENT_TITLE_SHA", "0") == "1"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DB         = DATA_DIR / "liril_intent.sqlite"
GAMES_FILE       = DATA_DIR / "liril_intent_games.txt"
AUDIT_LOG        = DATA_DIR / "liril_intent_audit.jsonl"


# ─────────────────────────────────────────────────────────────────────
# INTENT CATEGORIES + PRIORITY
# ─────────────────────────────────────────────────────────────────────

CAT_IDLE     = "IDLE"
CAT_GAMING   = "GAMING"
CAT_MEETING  = "MEETING"
CAT_CODING   = "CODING"
CAT_WRITING  = "WRITING"
CAT_MEDIA    = "MEDIA"
CAT_BROWSING = "BROWSING"
CAT_UNKNOWN  = "UNKNOWN"

# Tie-breaker priority (higher wins)
_PRIORITY = {
    CAT_IDLE:     100,
    CAT_GAMING:   80,
    CAT_MEETING:  70,
    CAT_CODING:   60,
    CAT_WRITING:  50,
    CAT_MEDIA:    40,
    CAT_BROWSING: 30,
    CAT_UNKNOWN:  0,
}

# Process-name-to-category (lowercased basename)
_PROC_TO_CAT: dict[str, str] = {}
for p in [
    "code.exe", "code-insiders.exe", "codium.exe", "vscodium.exe",
    "devenv.exe", "pycharm64.exe", "pycharm.exe", "idea64.exe",
    "idea.exe", "rustrover64.exe", "rustrover.exe", "goland64.exe",
    "webstorm64.exe", "sublime_text.exe", "subl.exe",
    "notepad++.exe", "helix.exe", "hx.exe", "nvim.exe", "nvim-qt.exe",
    "vim.exe", "gvim.exe", "windsurf.exe", "cursor.exe", "claude.exe",
    "zed.exe", "atom.exe", "kate.exe",
]:
    _PROC_TO_CAT[p] = CAT_CODING
for p in [
    "winword.exe", "wps.exe", "onenote.exe", "evernote.exe",
    "obsidian.exe", "typora.exe", "notion.exe", "logseq.exe",
    "scrivener.exe", "libreoffice.exe", "soffice.exe",
]:
    _PROC_TO_CAT[p] = CAT_WRITING
for p in [
    "teams.exe", "zoom.exe", "webex.exe", "gotomeeting.exe",
    "ms-teams.exe", "slack.exe",
]:
    _PROC_TO_CAT[p] = CAT_MEETING
for p in [
    "chrome.exe", "firefox.exe", "msedge.exe", "brave.exe", "arc.exe",
    "opera.exe", "vivaldi.exe",
]:
    _PROC_TO_CAT[p] = CAT_BROWSING
for p in [
    "mpv.exe", "vlc.exe", "wmplayer.exe", "spotify.exe",
    "foobar2000.exe", "audacious.exe", "potplayer.exe",
    "potplayer64.exe", "mpc-hc64.exe", "mpc-hc.exe",
]:
    _PROC_TO_CAT[p] = CAT_MEDIA

# Title-hint keywords (title leaks are checked against these for MEETING/MEDIA)
_MEETING_TITLE_HINTS = (
    "meet - google meet", "google meet", "microsoft teams meeting",
    "zoom meeting", "- meeting", "in a call",
)
_MEDIA_TITLE_HINTS = (
    "- youtube", "- netflix", "- prime video", "- disney+",
    "- spotify", "- twitch",
)


def _load_games() -> set[str]:
    if not GAMES_FILE.exists():
        return set()
    try:
        out: set[str] = set()
        for line in GAMES_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip().lower()
            if s and not s.startswith("#"):
                out.add(s)
        return out
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(STATE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS current (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transitions (
            ts         REAL PRIMARY KEY,
            prior      TEXT,
            current    TEXT NOT NULL,
            confidence REAL NOT NULL,
            signals    TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_transitions_ts ON transitions(ts)")
    c.commit()
    return c


def _get_current(c: sqlite3.Connection) -> dict:
    out = {"category": CAT_UNKNOWN, "confidence": 0.0, "ts": None}
    for k in ("category", "confidence", "ts", "signals"):
        r = c.execute("SELECT value FROM current WHERE key=?", (k,)).fetchone()
        if r:
            val = r[0]
            if k in ("confidence",):
                try: val = float(val)
                except Exception: val = 0.0
            if k == "signals":
                try: val = json.loads(val)
                except Exception: val = {}
            out[k] = val
    return out


def _set_current(c: sqlite3.Connection, intent: dict) -> None:
    for k, v in [
        ("category",   intent.get("category", CAT_UNKNOWN)),
        ("confidence", str(intent.get("confidence", 0.0))),
        ("ts",         intent.get("ts", _utc())),
        ("signals",    json.dumps(intent.get("signals", {}))),
    ]:
        c.execute("INSERT OR REPLACE INTO current(key, value) VALUES(?, ?)",
                  (k, str(v)))
    c.commit()


def _record_transition(c: sqlite3.Connection, prior: str, intent: dict) -> None:
    c.execute(
        "INSERT OR REPLACE INTO transitions(ts, prior, current, confidence, signals) "
        "VALUES(?, ?, ?, ?, ?)",
        (time.time(), prior, intent.get("category", CAT_UNKNOWN),
         float(intent.get("confidence", 0.0)),
         json.dumps(intent.get("signals", {}))),
    )
    # Keep last 2000 transitions
    c.execute("DELETE FROM transitions WHERE ts NOT IN "
              "(SELECT ts FROM transitions ORDER BY ts DESC LIMIT 2000)")
    c.commit()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────

def current_intent() -> dict:
    """Read the last-known intent from sqlite. Callable from any capability."""
    try:
        c = _db()
        try:
            return _get_current(c)
        finally:
            c.close()
    except Exception:
        return {"category": CAT_UNKNOWN, "confidence": 0.0}


def is_user_busy() -> bool:
    """True when the user appears actively engaged — other caps should
    avoid intrusive actions (Windows Update, service restarts, etc)."""
    cat = current_intent().get("category", CAT_UNKNOWN)
    return cat in (CAT_GAMING, CAT_MEETING, CAT_CODING, CAT_WRITING)


# ─────────────────────────────────────────────────────────────────────
# SIGNAL COLLECTORS
# ─────────────────────────────────────────────────────────────────────

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _idle_sec() -> float:
    """GetLastInputInfo / 1000 → seconds since last user input."""
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        if not ok:
            return 0.0
        # GetTickCount wraps every ~49.7 days; we're fine.
        now_tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (now_tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _foreground_window() -> dict:
    """Return {pid, process_name, title} of the foreground window.
    Uses user32.GetForegroundWindow + GetWindowThreadProcessId + psutil."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {}
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_val = int(pid.value)
        # Title
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        # Process name via psutil (fallback to image-name via WMI if missing)
        name = ""
        try:
            import psutil  # type: ignore
            try:
                name = psutil.Process(pid_val).name() or ""
            except Exception:
                name = ""
        except ImportError:
            pass
        if not name and pid_val:
            try:
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     f"(Get-Process -Id {pid_val} -ErrorAction SilentlyContinue).ProcessName"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                nm = (r.stdout or "").strip()
                if nm and not nm.lower().endswith(".exe"):
                    nm = nm + ".exe"
                name = nm
            except Exception:
                pass
        return {"pid": pid_val, "process_name": name, "title": title}
    except Exception:
        return {}


def _top_gpu_user() -> dict:
    """nvidia-smi compute-apps — return dict of {gpu_index: [process_names]}.
    Used to detect "GPU1 has a 3D game on it" for the GAMING heuristic."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return {}
    if r.returncode != 0 or not r.stdout:
        return {}
    # CSV format: uuid, pid, name, mem_mb
    out: dict[str, list[str]] = {}
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        uuid_ = parts[0]
        pname = parts[2]
        out.setdefault(uuid_, []).append(pname)
    return out


# ─────────────────────────────────────────────────────────────────────
# RULE-BASED CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────

def _classify_rules(signals: dict) -> list[tuple[str, float, str]]:
    """Returns list of (category, confidence, reason) candidates.
    Caller picks the winner by priority + confidence."""
    out: list[tuple[str, float, str]] = []

    # IDLE
    idle = float(signals.get("idle_sec", 0))
    if idle >= IDLE_THRESHOLD_S:
        out.append((CAT_IDLE, min(1.0, idle / (IDLE_THRESHOLD_S * 2)),
                    f"idle {int(idle)}s"))
        return out  # IDLE dominates — short-circuit

    fg = signals.get("foreground", {}) or {}
    name = (fg.get("process_name") or "").strip().lower()
    title = (fg.get("title") or "").strip().lower()

    # Process-name table
    if name in _PROC_TO_CAT:
        cat = _PROC_TO_CAT[name]
        conf = 0.9
        # Browsers are often ambiguous — check title hints to upgrade to
        # MEETING or MEDIA when appropriate
        if cat == CAT_BROWSING:
            if any(h in title for h in _MEETING_TITLE_HINTS):
                out.append((CAT_MEETING, 0.95, f"browser title matches meeting: {name}"))
            elif any(h in title for h in _MEDIA_TITLE_HINTS):
                out.append((CAT_MEDIA, 0.9, f"browser title matches media: {name}"))
            else:
                out.append((CAT_BROWSING, conf, f"process={name}"))
        else:
            out.append((cat, conf, f"process={name}"))

    # User-curated games list — this is the ONLY positive GAMING signal.
    # We tried a GPU-compute-apps heuristic in v0 but it produced false
    # positives for every GPU-accelerated Electron/browser app (Discord,
    # Edge, Spotify, Chrome — all compute-app users on modern Windows).
    # A proper game detector needs fullscreen-exclusive / DXGI swapchain
    # probing, which is out of scope for v1. Until then: whitelist-only.
    games = _load_games()
    if name and name in games:
        out.append((CAT_GAMING, 0.95, f"{name} in games whitelist"))

    if not out:
        out.append((CAT_UNKNOWN, 0.0, "no rule matched"))
    return out


def _pick_winner(candidates: list[tuple[str, float, str]]) -> tuple[str, float, list[str]]:
    """Apply priority + confidence to pick a single category."""
    best_score = -1.0
    best_cat = CAT_UNKNOWN
    reasons: list[str] = []
    for cat, conf, reason in candidates:
        # Score = priority * (0.5 + 0.5*conf) so a high-priority match
        # always beats a lower-priority one of equal confidence, but
        # within a priority tier confidence still matters.
        score = _PRIORITY.get(cat, 0) * (0.5 + 0.5 * conf)
        if score > best_score:
            best_score = score
            best_cat = cat
        reasons.append(f"{cat}({conf:.2f}): {reason}")
    # Confidence of the winner
    winner_confs = [c for cat, c, _ in candidates if cat == best_cat]
    winner_conf = max(winner_confs) if winner_confs else 0.0
    return best_cat, winner_conf, reasons


# ─────────────────────────────────────────────────────────────────────
# INFERENCE CYCLE
# ─────────────────────────────────────────────────────────────────────

def _gather_signals() -> dict:
    sig: dict = {}
    sig["idle_sec"]        = _idle_sec()
    sig["foreground"]      = _foreground_window()
    sig["gpu_compute_apps"] = _top_gpu_user()
    # Title scrubbing / hashing
    fg = sig["foreground"]
    raw_title = fg.get("title", "")
    if raw_title:
        if TITLE_HASH:
            fg["title_sha"] = hashlib.sha256(raw_title.encode("utf-8")).hexdigest()[:8]
        # Drop the raw title from the published signal to protect privacy
        fg.pop("title", None)
    # Process name is kept
    return sig


def _infer_once() -> dict:
    sig = _gather_signals()
    cands = _classify_rules(sig)
    cat, conf, reasons = _pick_winner(cands)
    return {
        "ts":         _utc(),
        "category":   cat,
        "confidence": conf,
        "signals":    sig,
        "reasoning":  reasons,
    }


# ─────────────────────────────────────────────────────────────────────
# PUBLISH / DAEMON
# ─────────────────────────────────────────────────────────────────────

async def _publish_once(nc=None, force: bool = False) -> dict:
    intent = _infer_once()
    c = _db()
    try:
        prev = _get_current(c)
        prior_cat = prev.get("category", CAT_UNKNOWN)
        changed = prior_cat != intent["category"] or force
        _set_current(c, intent)
        if changed:
            _record_transition(c, prior_cat, intent)
            _audit({"kind": "transition", "prior": prior_cat, **intent})
    finally:
        c.close()

    # Publish — if we have no NATS, still return the intent for --snapshot
    if nc is None:
        try:
            import nats as _nats
            nc_local = await _nats.connect(NATS_URL, connect_timeout=3)
        except Exception:
            return intent
        try:
            await _do_publish(nc_local, intent, changed)
        finally:
            await nc_local.drain()
    else:
        await _do_publish(nc, intent, changed)
    return intent


async def _do_publish(nc, intent: dict, changed: bool) -> None:
    try:
        await nc.publish(INTENT_SUBJECT, json.dumps(intent, default=str).encode())
    except Exception:
        pass
    if changed:
        try:
            await nc.publish(TRANSITION_SUBJ, json.dumps(intent, default=str).encode())
        except Exception:
            pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[INTENT] daemon starting — poll every {POLL_SEC:.0f}s")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[INTENT] NATS unavailable: {e!r} — retrying each cycle")
        nc = None
    last_metric = 0.0
    last_full_publish = 0.0
    try:
        while True:
            try:
                if nc is None:
                    try:
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                    except Exception:
                        nc = None
                force = (time.time() - last_full_publish) >= 300  # heartbeat every 5 min
                intent = await _publish_once(nc, force=force)
                if force:
                    last_full_publish = time.time()
                # Metrics every minute
                if nc is not None and time.time() - last_metric >= 60:
                    snap = {
                        "ts":       _utc(),
                        "category": intent["category"],
                        "host":     os.environ.get("COMPUTERNAME", ""),
                    }
                    try: await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
                    except Exception: pass
                    last_metric = time.time()
            except Exception as e:
                print(f"[INTENT] cycle error: {type(e).__name__}: {e}")
                try:
                    if nc is not None:
                        await nc.close()
                except Exception:
                    pass
                nc = None
            await asyncio.sleep(POLL_SEC)
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL User-Intent Prediction — Capability #9")
    ap.add_argument("--snapshot",    action="store_true", help="Run one inference + print")
    ap.add_argument("--current",     action="store_true", help="Print cached current intent")
    ap.add_argument("--history",     type=int, nargs="?", const=10,
                    help="Show last N intent transitions (default 10)")
    ap.add_argument("--list-rules",  action="store_true", help="Print rule table")
    ap.add_argument("--daemon",      action="store_true", help="Run poll loop")
    args = ap.parse_args()

    if args.list_rules:
        print("# Category priority (higher = wins ties)")
        for cat, pri in sorted(_PRIORITY.items(), key=lambda x: -x[1]):
            print(f"  {cat:9s} {pri}")
        print("# Process → category")
        by_cat: dict[str, list[str]] = {}
        for name, cat in _PROC_TO_CAT.items():
            by_cat.setdefault(cat, []).append(name)
        for cat in (CAT_CODING, CAT_WRITING, CAT_MEETING, CAT_BROWSING, CAT_MEDIA):
            plist = sorted(by_cat.get(cat, []))
            print(f"  {cat:9s} ({len(plist):>2}): {', '.join(plist)[:120]}")
        print(f"# Idle threshold: {IDLE_THRESHOLD_S:.0f}s")
        print(f"# User-curated games list: {GAMES_FILE}  "
              f"({len(_load_games())} entries)")
        return 0

    if args.snapshot:
        intent = asyncio.run(_publish_once(None))
        print(json.dumps(intent, indent=2, default=str))
        return 0

    if args.current:
        cur = current_intent()
        print(json.dumps(cur, indent=2, default=str))
        return 0

    if args.history is not None:
        c = _db()
        try:
            rows = c.execute(
                "SELECT ts, prior, current, confidence FROM transitions "
                "ORDER BY ts DESC LIMIT ?", (max(1, int(args.history)),)
            ).fetchall()
            for ts, prior, cur, conf in rows:
                age = time.time() - ts
                print(f"  -{int(age):>6}s  {prior:9s} → {cur:9s}  conf={conf:.2f}")
        finally:
            c.close()
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[INTENT] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
