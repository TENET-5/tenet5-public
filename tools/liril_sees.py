# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:20:00Z
"""liril_sees — LIRIL's eyes. CPU-only continuous filesystem + git watcher.

Runs always, publishes events on NATS so the thinking loop can react in
real time rather than on a timer.

Published subjects (NATS on 4223):
  tenet5.liril.see.file_change    — any *.json, *.html, *.js, *.md write
  tenet5.liril.see.new_commit     — git log tail detected a new commit
  tenet5.liril.see.transcript     — new dev-team cycle transcript appeared

Resource use: ~30 MB RAM, <1% CPU. Runs during gaming without GPU impact.

Graceful degradation: if the `watchfiles` library is not installed, falls
back to polling mtime-every-2-seconds. If NATS is unreachable, events
buffer to data/liril_perception_queue.jsonl and are replayed when NATS
comes back.
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
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("NATS_URL", "nats://127.0.0.1:4223")

import nats  # type: ignore

SITE = Path(r"E:\TENET-5.github.io")
QUEUE = SITE / "data" / "liril_perception_queue.jsonl"
LAST_COMMIT_FILE = SITE / "data" / ".liril_sees_last_commit"
WATCH_EXTS = {".json", ".jsonl", ".html", ".js", ".md", ".css"}
IGNORE_GLOBS = ("/.git/", "/node_modules/", "/__pycache__/", "/_PAUSED_REDDUSTER/")


def _log(tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] [SEES/{tag}] {msg}", flush=True)


async def publish(nc, subject: str, payload: dict) -> None:
    """Publish with on-disk queue fallback if NATS is down."""
    try:
        await nc.publish(subject, json.dumps(payload).encode("utf-8"))
    except Exception as e:
        _log("QUEUE", f"NATS publish failed ({e!r}); writing to disk queue")
        with QUEUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"subject": subject, **payload}, ensure_ascii=False) + "\n")


async def drain_queue(nc) -> int:
    """Replay queued events when NATS is back."""
    if not QUEUE.exists() or QUEUE.stat().st_size == 0:
        return 0
    lines = QUEUE.read_text(encoding="utf-8").splitlines()
    QUEUE.write_text("", encoding="utf-8")
    replayed = 0
    for L in lines:
        L = L.strip()
        if not L:
            continue
        try:
            e = json.loads(L)
        except Exception:
            continue
        subj = e.pop("subject", "tenet5.liril.see.unknown")
        try:
            await nc.publish(subj, json.dumps(e).encode("utf-8"))
            replayed += 1
        except Exception:
            with QUEUE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"subject": subj, **e}, ensure_ascii=False) + "\n")
            return replayed
    return replayed


def should_watch(path: Path) -> bool:
    p = str(path).replace("\\", "/").lower()
    if any(g in p for g in IGNORE_GLOBS):
        return False
    return path.suffix.lower() in WATCH_EXTS


async def watch_filesystem(nc) -> None:
    """Try watchfiles (efficient); fall back to polling if not installed."""
    try:
        from watchfiles import awatch, Change
        _log("FS", f"watchfiles backend on {SITE}")
        async for changes in awatch(str(SITE), recursive=True):
            for change_type, changed_path in changes:
                p = Path(changed_path)
                if not should_watch(p):
                    continue
                rel = str(p.relative_to(SITE)).replace("\\", "/")
                event = {
                    "ts":         int(time.time()),
                    "path":       rel,
                    "ext":        p.suffix.lower(),
                    "change":     change_type.name if hasattr(change_type, "name") else str(change_type),
                    "size_bytes": p.stat().st_size if p.exists() else None,
                }
                await publish(nc, "tenet5.liril.see.file_change", event)
    except ImportError:
        _log("FS", "watchfiles not installed; polling mtime every 2s")
        mtimes: dict[str, float] = {}
        while True:
            for ext in WATCH_EXTS:
                for p in SITE.rglob(f"*{ext}"):
                    if not should_watch(p):
                        continue
                    try:
                        m = p.stat().st_mtime
                    except OSError:
                        continue
                    rel = str(p.relative_to(SITE)).replace("\\", "/")
                    prev = mtimes.get(rel)
                    if prev is None:
                        mtimes[rel] = m
                    elif m > prev:
                        mtimes[rel] = m
                        event = {
                            "ts": int(time.time()),
                            "path": rel,
                            "ext": ext,
                            "change": "modified",
                            "size_bytes": p.stat().st_size,
                        }
                        await publish(nc, "tenet5.liril.see.file_change", event)
            await asyncio.sleep(2.0)


async def watch_git(nc) -> None:
    """Poll `git log -1` every 5s; publish when HEAD advances."""
    last = LAST_COMMIT_FILE.read_text(encoding="utf-8").strip() if LAST_COMMIT_FILE.exists() else ""
    while True:
        try:
            r = subprocess.run(
                ["git", "-C", str(SITE), "log", "-1", "--pretty=format:%H|%an|%at|%s"],
                capture_output=True, encoding="utf-8", errors="replace", timeout=10
            )
            line = (r.stdout or "").strip()
        except Exception:
            line = ""
        if line and "|" in line:
            sha, author, ts, subject = line.split("|", 3)
            if sha != last:
                event = {
                    "ts":        int(ts),
                    "sha":       sha,
                    "author":    author,
                    "subject":   subject,
                    "is_autonomous": subject.startswith("autonomous("),
                    "is_jargon":     any(k in subject for k in ("[PHASE ", "[LIRIL PHASE ", "ABCXYZ", "Millennial Falcon")),
                }
                await publish(nc, "tenet5.liril.see.new_commit", event)
                _log("GIT", f"new commit {sha[:10]} by {author}: {subject[:60]}")
                last = sha
                LAST_COMMIT_FILE.write_text(sha, encoding="utf-8")
        await asyncio.sleep(5)


async def watch_transcripts(nc) -> None:
    """Watch data/liril_dev_team_log/ for new transcript files."""
    log_dir = SITE / "data" / "liril_dev_team_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set(p.name for p in log_dir.glob("*.json"))
    while True:
        current = {p.name for p in log_dir.glob("*.json")}
        for name in sorted(current - seen):
            p = log_dir / name
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                event = {
                    "ts":       int(p.stat().st_mtime),
                    "path":     f"data/liril_dev_team_log/{name}",
                    "task_id":  d.get("task", {}).get("id"),
                    "status":   d.get("status"),
                    "verdict":  d.get("verdict"),
                    "role_count": len(d.get("roles", [])),
                }
                await publish(nc, "tenet5.liril.see.transcript", event)
                _log("TRANSCRIPT", f"new: {name}  verdict={event.get('verdict')}")
            except Exception as e:
                _log("TRANSCRIPT", f"error parsing {name}: {e}")
        seen = current
        await asyncio.sleep(3)


async def main() -> None:
    _log("BOOT", f"NATS_URL={os.environ['NATS_URL']}  watching {SITE}")
    while True:
        try:
            nc = await nats.connect(os.environ["NATS_URL"], connect_timeout=5, reconnect_time_wait=2, max_reconnect_attempts=-1)
            replayed = await drain_queue(nc)
            if replayed:
                _log("QUEUE", f"replayed {replayed} queued events")
            await asyncio.gather(
                watch_filesystem(nc),
                watch_git(nc),
                watch_transcripts(nc),
            )
        except Exception as e:
            _log("ERROR", f"top-level {e!r}; sleep 5 and reconnect")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
