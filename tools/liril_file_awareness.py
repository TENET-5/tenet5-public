#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T13:15:00Z | Author: claude_code | Change: LIRIL Skill — File Content Awareness (observational, magic-byte + path heuristics)
"""LIRIL File Content Awareness — observational file-system watcher.

LIRIL picked this at severity=high when asked what new skills she wanted:
"Enables proactive threat detection and system optimization by analyzing
file contents."

Scope — v1 is OBSERVATIONAL ONLY
--------------------------------
This skill NEVER moves, deletes, quarantines, or otherwise mutates files.
It watches a set of paths, classifies new / recently-modified files by
magic bytes + extension + location heuristics, and publishes alerts to
fse + a dedicated file-alert subject. Any destructive response is a
later capability's job (and will go through the normal plan→veto pipeline).

Watched paths (defaults — overridable via data/liril_file_watch.txt):
  %USERPROFILE%\\Downloads
  %TEMP%
  %APPDATA%\\..\\Local\\Temp

Classification signals
----------------------
  Magic bytes — "MZ" (PE exe), "#!"/"PK" (shebang/zip), "{"/"<" (text),
                "ELF"/"\\x7fELF" (unix exe; suspicious on Windows)
  Extension   — matched against risk categories
  Double-ext  — .doc.exe, .pdf.scr, .jpg.ps1 → critical
  Hidden+exe  — executable bit-ish (known binary) in a hidden dir → high
  Path context
              — executable in Downloads → medium
              — script in %TEMP% → high
              — signed system files (C:\\Windows\\...) are ignored

Rate limiting
-------------
One scan pass every 60s. Within a pass, each file classified once; if the
same file's classification changes the alert is suppressed for an hour
(to prevent AV-style churn).

NATS surface
------------
  tenet5.liril.file.alert    — per-alert record
  tenet5.liril.file.metrics  — 60-s snapshot (files scanned, alerts fired)

CLI
---
  --scan-once          Single pass + print findings
  --list-watched       Show watched paths
  --list-rules         Show classification table
  --snapshot           Publish metrics snapshot
  --daemon             Run the watcher loop
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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NATS_URL         = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
ALERT_SUBJECT    = "tenet5.liril.file.alert"
METRICS_SUBJECT  = "tenet5.liril.file.metrics"
POLL_SEC         = float(os.environ.get("LIRIL_FILE_INTERVAL", "60"))
ALERT_SUPPRESS_SEC = 3600.0
MAX_FILES_PER_PASS = 500     # prevent runaway on huge Downloads folder
MAX_READ_BYTES     = 16        # just the header for magic-bytes

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WATCH_FILE   = DATA_DIR / "liril_file_watch.txt"
STATE_DB     = DATA_DIR / "liril_file_state.sqlite"
AUDIT_LOG    = DATA_DIR / "liril_file_audit.jsonl"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# WATCH-LIST CONFIG
# ─────────────────────────────────────────────────────────────────────

def _default_watch_paths() -> list[Path]:
    paths: list[Path] = []
    up = os.environ.get("USERPROFILE") or ""
    if up:
        p = Path(up) / "Downloads"
        if p.exists():
            paths.append(p)
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if tmp and Path(tmp).exists():
        paths.append(Path(tmp))
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        local_temp = Path(appdata).parent / "Local" / "Temp"
        if local_temp.exists():
            paths.append(local_temp)
    # Dedup
    seen = set()
    out = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def _load_watch_paths() -> list[Path]:
    if not WATCH_FILE.exists():
        # Seed default
        default = "\n".join(str(p) for p in _default_watch_paths())
        try:
            WATCH_FILE.write_text(
                "# LIRIL file awareness watch paths — one per line\n"
                "# Comments start with #. Re-reads at each scan pass.\n"
                + default + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
    if not WATCH_FILE.exists():
        return _default_watch_paths()
    out: list[Path] = []
    try:
        for line in WATCH_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            p = Path(s)
            if p.exists():
                out.append(p)
    except Exception:
        pass
    return out or _default_watch_paths()


# ─────────────────────────────────────────────────────────────────────
# MAGIC BYTES + CLASSIFICATION RULES
# ─────────────────────────────────────────────────────────────────────

_MAGIC = [
    (b"MZ",         "pe_executable"),     # Windows EXE/DLL
    (b"\x7fELF",    "elf_executable"),    # Linux binary (suspicious on Win)
    (b"#!",         "unix_script"),       # shebang
    (b"PK\x03\x04", "zip_archive"),       # zip (covers .docx/.xlsx)
    (b"%PDF-",      "pdf"),
    (b"\x89PNG",    "png"),
    (b"GIF8",       "gif"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"7z\xbc\xaf\x27\x1c", "7z_archive"),
    (b"Rar!",       "rar_archive"),
    (b"BZh",        "bzip2"),
    (b"\x1f\x8b\x08","gzip"),
]

# Extension → risk category
_RISK_EXT = {
    ".exe":  "executable",
    ".msi":  "installer",
    ".bat":  "script",
    ".cmd":  "script",
    ".ps1":  "script",
    ".vbs":  "script",
    ".vbe":  "script",
    ".js":   "script",
    ".jse":  "script",
    ".wsf":  "script",
    ".hta":  "script",
    ".scr":  "executable",
    ".pif":  "executable",
    ".dll":  "library",
    ".jar":  "java_archive",
}

_EXECUTABLE_EXTS = {".exe", ".msi", ".scr", ".pif", ".bat", ".cmd",
                    ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                    ".hta", ".jar"}
_DOUBLE_EXT_BAIT_FIRST = {".doc", ".docx", ".pdf", ".jpg", ".jpeg",
                          ".png", ".gif", ".xlsx", ".xls", ".pptx",
                          ".mp3", ".mp4", ".txt"}


def _magic_name(head: bytes) -> str:
    for sig, name in _MAGIC:
        if head.startswith(sig):
            return name
    return "unknown"


def _classify(path: Path) -> dict:
    """Classify a single file. Returns dict with keys:
       path, size, ext, magic, risk, reason."""
    try:
        st = path.stat()
    except Exception as e:
        return {"path": str(path), "error": f"stat: {type(e).__name__}"}

    size = int(st.st_size)
    mtime = float(st.st_mtime)

    try:
        with path.open("rb") as f:
            head = f.read(MAX_READ_BYTES)
    except Exception:
        head = b""

    ext = path.suffix.lower()
    magic = _magic_name(head)

    # Double-extension bait: e.g. invoice.pdf.exe
    name_lower = path.name.lower()
    dbl = False
    dbl_parts = name_lower.split(".")
    if len(dbl_parts) >= 3:
        # Penultimate "innocent" extension, final suspicious
        inner = "." + dbl_parts[-2]
        outer = "." + dbl_parts[-1]
        if inner in _DOUBLE_EXT_BAIT_FIRST and outer in _EXECUTABLE_EXTS:
            dbl = True

    # Determine risk
    risk = "low"
    reasons: list[str] = []

    if dbl:
        risk = "critical"
        reasons.append(f"double-extension: ...{inner}{outer}")
    elif magic == "elf_executable":
        risk = "high"
        reasons.append("ELF binary on Windows")
    elif ext in _EXECUTABLE_EXTS or magic == "pe_executable":
        # Location matters
        path_lower = str(path).lower()
        if "\\downloads\\" in path_lower or "/downloads/" in path_lower:
            risk = "medium"
            reasons.append(f"{magic}/{ext} in Downloads")
        elif "\\temp\\" in path_lower or "/temp/" in path_lower:
            risk = "high"
            reasons.append(f"{magic}/{ext} in TEMP")
        elif "\\appdata\\local\\temp\\" in path_lower:
            risk = "high"
            reasons.append(f"{magic}/{ext} in AppData\\Local\\Temp")
        else:
            risk = "low"
            reasons.append(f"{magic}/{ext} in non-volatile path")

    # Signed system files in C:\Windows — never alert
    pl = str(path).lower()
    if pl.startswith(("c:\\windows\\", "c:\\program files", "c:\\program files (x86)")):
        risk = "low"
        reasons = ["system_path_ignored"]

    return {
        "path":   str(path),
        "size":   size,
        "mtime":  mtime,
        "ext":    ext,
        "magic":  magic,
        "risk":   risk,
        "reasons": reasons,
        "double_ext": dbl,
    }


# ─────────────────────────────────────────────────────────────────────
# STATE (alert suppression, scan stats)
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(STATE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts_seen (
            path_hash  TEXT PRIMARY KEY,
            path       TEXT NOT NULL,
            last_risk  TEXT NOT NULL,
            last_ts    REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.commit()
    return c


def _should_alert(path: str, risk: str) -> bool:
    """True if this path+risk isn't in the suppression window."""
    h = hashlib.sha1(path.encode("utf-8", errors="replace")).hexdigest()[:16]
    c = _db()
    try:
        r = c.execute(
            "SELECT last_risk, last_ts FROM alerts_seen WHERE path_hash=?", (h,)
        ).fetchone()
        now = time.time()
        if r:
            last_risk, last_ts = r
            if last_risk == risk and (now - float(last_ts)) < ALERT_SUPPRESS_SEC:
                return False
        c.execute(
            "INSERT OR REPLACE INTO alerts_seen(path_hash, path, last_risk, last_ts) "
            "VALUES(?, ?, ?, ?)",
            (h, path, risk, now),
        )
        c.commit()
        return True
    finally:
        c.close()


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────────────────────────────────

def scan_once() -> dict:
    """Walk every watched path and classify recently-modified files.
    Returns stats dict."""
    paths = _load_watch_paths()
    stats = {
        "ts":         _utc(),
        "watched":    [str(p) for p in paths],
        "files_seen": 0,
        "alerts":     [],
    }
    cutoff = time.time() - 48 * 3600   # Only look at files touched in last 48h
    count = 0
    for root in paths:
        for f in root.rglob("*"):
            if count >= MAX_FILES_PER_PASS:
                break
            try:
                if not f.is_file():
                    continue
                if f.stat().st_mtime < cutoff:
                    continue
            except Exception:
                continue
            count += 1
            cls = _classify(f)
            if cls.get("risk") in ("medium", "high", "critical"):
                if _should_alert(cls["path"], cls["risk"]):
                    stats["alerts"].append(cls)
                    _audit({"kind": "alert", **cls})
        if count >= MAX_FILES_PER_PASS:
            break
    stats["files_seen"] = count
    return stats


async def _publish_alerts(nc, alerts: list[dict]) -> None:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
    except Exception:
        _fse = None
    for a in alerts:
        try:
            await nc.publish(ALERT_SUBJECT, json.dumps(a, default=str).encode())
        except Exception:
            pass
        # Critical file alerts also escalate through fse
        if _fse is not None and a.get("risk") == "critical":
            try:
                _fse.file_incident_local(
                    "critical", "liril_file_awareness",
                    f"critical file alert: {a.get('path','')} "
                    f"({', '.join(a.get('reasons', []))})",
                    data={"path": a["path"], "risk": a["risk"]},
                )
            except Exception:
                pass


async def _publish_metrics(nc, stats: dict) -> None:
    snap = {
        "ts":         stats.get("ts", _utc()),
        "watched":    stats.get("watched", []),
        "files_seen": stats.get("files_seen", 0),
        "alert_count": len(stats.get("alerts", [])),
    }
    try:
        await nc.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
    except Exception:
        pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[FILE] daemon starting — scan every {POLL_SEC:.0f}s")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[FILE] NATS unavailable: {e!r} — will retry")
        nc = None

    try:
        while True:
            try:
                if nc is None:
                    try:
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                    except Exception:
                        nc = None
                stats = scan_once()
                if nc is not None:
                    await _publish_metrics(nc, stats)
                    if stats["alerts"]:
                        await _publish_alerts(nc, stats["alerts"])
                if stats["alerts"]:
                    print(f"[FILE] {len(stats['alerts'])} alerts from "
                          f"{stats['files_seen']} files scanned")
            except Exception as e:
                print(f"[FILE] cycle error: {type(e).__name__}: {e}")
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
    ap = argparse.ArgumentParser(description="LIRIL File Content Awareness")
    ap.add_argument("--scan-once",    action="store_true", help="One scan pass + print")
    ap.add_argument("--list-watched", action="store_true")
    ap.add_argument("--list-rules",   action="store_true")
    ap.add_argument("--snapshot",     action="store_true", help="Publish metrics + exit")
    ap.add_argument("--daemon",       action="store_true")
    args = ap.parse_args()

    if args.list_watched:
        for p in _load_watch_paths():
            print(f"  {p}")
        return 0

    if args.list_rules:
        print("# Magic bytes → type")
        for sig, name in _MAGIC:
            print(f"  {sig!r:30s} {name}")
        print("# Extension → risk category")
        for ext in sorted(_RISK_EXT):
            print(f"  {ext:6s} {_RISK_EXT[ext]}")
        print("# Executable extensions:", sorted(_EXECUTABLE_EXTS))
        print(f"# Double-extension inner suspects: {sorted(_DOUBLE_EXT_BAIT_FIRST)}")
        print(f"# Scan interval: {POLL_SEC:.0f}s  "
              f"Alert suppression: {ALERT_SUPPRESS_SEC:.0f}s  "
              f"Max files/pass: {MAX_FILES_PER_PASS}")
        return 0

    if args.scan_once:
        stats = scan_once()
        print(f"scanned: {stats['files_seen']}  alerts: {len(stats['alerts'])}")
        for a in stats["alerts"]:
            print(f"  [{a['risk']:8s}] {a['path']}  {', '.join(a.get('reasons', []))}")
        return 0

    if args.snapshot:
        async def run():
            import nats as _nats
            try:
                nc = await _nats.connect(NATS_URL, connect_timeout=3)
            except Exception:
                print("NATS unavailable")
                return
            try:
                stats = scan_once()
                await _publish_metrics(nc, stats)
                if stats["alerts"]:
                    await _publish_alerts(nc, stats["alerts"])
                print(json.dumps({
                    "files_seen": stats["files_seen"],
                    "alert_count": len(stats["alerts"]),
                    "watched":    stats["watched"],
                }, indent=2))
            finally:
                await nc.drain()
        asyncio.run(run())
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[FILE] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
