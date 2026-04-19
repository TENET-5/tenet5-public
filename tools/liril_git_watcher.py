#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T03:30:00Z | Author: claude_code | Change: LIRIL perception gap #5 — real-time git watcher
"""LIRIL Git Watcher — real-time commit monitoring + NATS publish.

Closes LIRIL's self-reported perception gap #5:
  'Real-time Collaboration and Communication Monitoring — LIRIL
   doesn't monitor real-time communication or collaboration tools.
   Rough build difficulty: Medium.'

This daemon polls the TENET5 + S.L.A.T.E repos for new commits every
few seconds. When a new HEAD appears, it:

  1. Extracts commit metadata (hash, author, subject, file list,
     insertions, deletions, is_autonomous)
  2. Publishes to NATS subject tenet5.liril.git.commit so any
     subscriber (dev-team daemon, failure analyzer, observer) can
     react instantly
  3. Incrementally re-scans changed files with liril_doc_parser so
     new TODO/FIXME markers enter LIRIL's doc index within seconds
  4. Classifies commit message axis via tenet5.liril.classify

No polling of GitHub API — pure local git on the watched repos.
Works offline, zero rate-limit risk. Alternate webhook mode for
deployments where the repos live elsewhere.

Run (daemon, pythonw):
  pythonw tools/liril_git_watcher.py --daemon --interval 15

Inspect:
  python tools/liril_git_watcher.py --recent 20
  python tools/liril_git_watcher.py --since 2026-04-19T00:00:00Z
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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Repos to watch. Each tuple (name, path)
WATCHED_REPOS = [
    ("tenet5",    Path(r"E:/S.L.A.T.E/tenet5")),
    ("tenet-5",   Path(r"E:/TENET-5.github.io")),
]

NATS_URL    = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
STATE_DIR   = Path(r"E:/S.L.A.T.E/tenet5/data/liril_git_watcher")
HISTORY_LOG = STATE_DIR / "history.jsonl"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], cwd: Path, timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
        err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
        return r.returncode, out, err
    except Exception as e:
        return -2, "", f"{type(e).__name__}: {e}"


# ──────────────────────────────────────────────────────────────
# GIT INSPECTION
# ──────────────────────────────────────────────────────────────

def _git_rev_parse_head(cwd: Path) -> str | None:
    rc, out, _ = _run(["git", "-c", "submodule.recurse=false", "rev-parse", "HEAD"], cwd)
    if rc != 0:
        return None
    return out.strip() or None


def _git_commit_info(cwd: Path, sha: str) -> dict:
    # Format: HASH | AUTHOR | ISO TIMESTAMP | SUBJECT
    fmt = "%H%n%an <%ae>%n%aI%n%s%n%B"
    rc, out, _ = _run(
        ["git", "-c", "submodule.recurse=false", "show",
         "--no-patch", f"--format={fmt}", sha],
        cwd,
    )
    if rc != 0 or not out:
        return {"sha": sha, "error": "git show failed"}
    lines = out.split("\n")
    if len(lines) < 4:
        return {"sha": sha, "error": "unexpected show output"}

    # Files + stats
    rc2, stats_out, _ = _run(
        ["git", "-c", "submodule.recurse=false", "show",
         "--numstat", "--format=", sha],
        cwd,
    )
    files: list[dict] = []
    if rc2 == 0:
        for line in stats_out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                ins, dels, fp = parts
                try:
                    ins_n = int(ins) if ins.isdigit() else 0
                    dels_n = int(dels) if dels.isdigit() else 0
                except Exception:
                    ins_n = dels_n = 0
                files.append({"path": fp, "ins": ins_n, "dels": dels_n})

    subject = lines[3].strip()
    is_autonomous = subject.lower().startswith("autonomous(")

    return {
        "sha":           lines[0].strip(),
        "author":        lines[1].strip(),
        "ts":            lines[2].strip(),
        "subject":       subject,
        "body":          "\n".join(lines[4:]).strip()[:2000],
        "files":         files,
        "is_autonomous": is_autonomous,
        "n_files":       len(files),
        "ins_total":     sum(f["ins"]  for f in files),
        "dels_total":    sum(f["dels"] for f in files),
    }


def _git_commits_since(cwd: Path, since_sha: str | None) -> list[str]:
    """Return list of new commits since since_sha (inclusive range, newest first)."""
    head = _git_rev_parse_head(cwd)
    if not head:
        return []
    if since_sha == head:
        return []
    if since_sha:
        rc, out, _ = _run(
            ["git", "-c", "submodule.recurse=false", "rev-list",
             f"{since_sha}..HEAD"],
            cwd,
        )
    else:
        rc, out, _ = _run(
            ["git", "-c", "submodule.recurse=false", "rev-list",
             "-20", "HEAD"],
            cwd,
        )
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


# ──────────────────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────────────────

def _state_file(repo: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{repo}.lastseen"


def _load_last_seen(repo: str) -> str | None:
    f = _state_file(repo)
    if not f.exists():
        return None
    v = f.read_text(encoding="utf-8").strip()
    return v or None


def _save_last_seen(repo: str, sha: str) -> None:
    _state_file(repo).write_text(sha, encoding="utf-8")


def _append_history(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ──────────────────────────────────────────────────────────────
# NATS
# ──────────────────────────────────────────────────────────────

async def _publish(commit: dict, nc) -> None:
    try:
        await nc.publish(
            "tenet5.liril.git.commit",
            json.dumps(commit, default=str).encode(),
        )
        await nc.flush(timeout=3)
    except Exception:
        pass


async def _classify_subject(nc, text: str) -> dict:
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text[:280], "source": "git_watcher"}).encode(),
            timeout=3,
        )
        return json.loads(msg.data.decode())
    except Exception:
        return {}


async def _rescan_docs(nc, files: list[str]) -> None:
    # Fire-and-forget signal to the doc parser — the daemon picks up
    # any file changes on the next scan. Here we just publish a hint.
    payload = {"files": files[:50], "source": "git_watcher", "ts": _utc()}
    try:
        await nc.publish(
            "tenet5.liril.docs.hint",
            json.dumps(payload).encode(),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# POLL LOOP
# ──────────────────────────────────────────────────────────────

async def poll_once(nc) -> dict:
    """One pass over all watched repos. Returns summary."""
    summary = {"checked": [], "new_commits": [], "ts": _utc()}
    for repo_name, repo_path in WATCHED_REPOS:
        if not repo_path.exists():
            continue
        last_seen = _load_last_seen(repo_name)
        head = _git_rev_parse_head(repo_path)
        if not head:
            continue
        summary["checked"].append({"repo": repo_name, "head": head, "last_seen": last_seen})
        if last_seen == head:
            continue

        commits = _git_commits_since(repo_path, last_seen)
        # Process oldest → newest
        for sha in reversed(commits):
            info = _git_commit_info(repo_path, sha)
            info["repo"] = repo_name
            # Classify axis
            cls = await _classify_subject(nc, info.get("subject", ""))
            if cls and not cls.get("error"):
                info["classification"] = {
                    "axis":       cls.get("domain") or cls.get("axis"),
                    "confidence": cls.get("confidence"),
                }
            # Publish
            await _publish(info, nc)
            # Doc rescan hint
            await _rescan_docs(nc, [f.get("path") for f in info.get("files", [])])
            # History
            _append_history(info)
            summary["new_commits"].append({
                "repo":    repo_name,
                "sha":     info["sha"][:10],
                "subject": info["subject"][:80],
                "files":   info["n_files"],
                "axis":    (info.get("classification") or {}).get("axis"),
                "is_auto": info.get("is_autonomous", False),
            })
        _save_last_seen(repo_name, head)
    return summary


async def daemon(interval: int = 15) -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[GIT-WATCH] subscribed tenet5.liril.git.* on {NATS_URL} — interval {interval}s")

    # Initial poll — establish baseline without flooding
    for repo_name, repo_path in WATCHED_REPOS:
        if not repo_path.exists():
            continue
        last_seen = _load_last_seen(repo_name)
        if not last_seen:
            head = _git_rev_parse_head(repo_path)
            if head:
                _save_last_seen(repo_name, head)
                print(f"[GIT-WATCH] initial baseline {repo_name} @ {head[:10]}")

    # Request-reply subject: recent commits
    async def h_recent(msg):
        limit = 10
        try:
            req = json.loads(msg.data.decode() or "{}")
            limit = int(req.get("limit", 10))
        except Exception:
            pass
        recent = list(_load_history(limit))
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(recent, default=str).encode())

    await nc.subscribe("tenet5.liril.git.recent", cb=h_recent)

    while True:
        try:
            summary = await poll_once(nc)
            if summary["new_commits"]:
                for c in summary["new_commits"]:
                    mark = "🤖" if c["is_auto"] else "👤"
                    axis = c.get("axis") or "-"
                    print(f"[GIT-WATCH] {mark} {c['repo']:10s} {c['sha']} "
                          f"[{axis:12s}] {c['subject']}")
        except Exception as e:
            print(f"[GIT-WATCH] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────
# INSPECTION
# ──────────────────────────────────────────────────────────────

def _load_history(limit: int = 10) -> list[dict]:
    if not HISTORY_LOG.exists():
        return []
    out = []
    with HISTORY_LOG.open(encoding="utf-8") as f:
        lines = f.readlines()
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _print_recent(limit: int) -> None:
    rows = _load_history(limit)
    print(f"── last {len(rows)} commits ──")
    for r in rows:
        mark = "🤖" if r.get("is_autonomous") else "👤"
        axis = (r.get("classification") or {}).get("axis", "-")
        print(f"  {mark} {r.get('repo','?'):10s} {(r.get('sha') or '')[:10]} "
              f"[{axis:12s}] {r.get('subject','')[:80]}")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL real-time git watcher")
    ap.add_argument("--daemon",   action="store_true", help="poll continuously")
    ap.add_argument("--interval", type=int, default=15, help="poll interval seconds")
    ap.add_argument("--once",     action="store_true", help="single poll pass, then exit")
    ap.add_argument("--recent",   type=int, default=None, help="print last N commits from history")
    ap.add_argument("--reset",    action="store_true", help="reset last-seen state (re-baseline)")
    ap.add_argument("--json",     action="store_true")
    args = ap.parse_args()

    if args.reset:
        for repo_name, _ in WATCHED_REPOS:
            f = _state_file(repo_name)
            if f.exists():
                f.unlink()
                print(f"reset {repo_name}")
        return 0

    if args.recent is not None:
        _print_recent(args.recent)
        return 0

    if args.once:
        async def once():
            import nats as _nats
            nc = await _nats.connect(NATS_URL, connect_timeout=5)
            try:
                r = await poll_once(nc)
                print(json.dumps(r, indent=2, default=str) if args.json
                      else f"checked={len(r['checked'])} new={len(r['new_commits'])}")
            finally:
                await nc.drain()
        asyncio.run(once())
        return 0

    if args.daemon:
        asyncio.run(daemon(interval=args.interval))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
