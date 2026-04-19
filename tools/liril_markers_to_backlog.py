#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T03:10:00Z | Author: claude_code | Change: markers → backlog bridge
"""Markers-to-backlog bridge — turn LIRIL's doc-parser index into
actionable dev-team tasks.

LIRIL picked this as the highest-leverage build post-doc-parser:
  'A) Build the markers-to-backlog bridge. This will immediately
   address … TODOs + CRITICALs by converting them into actionable
   tasks for the development team.'

Design call (against LIRIL's suggestion): we skip CRITICAL by default.
On this codebase most CRITICAL annotations mark descriptive logic
(e.g. 'Boost if Indicators are CRITICAL' in scoring rules, 'CRITICAL:
Do NOT call core.available_devices' in NPU-safety comments) — they're
emphasized documentation, not action items. Targeting CRITICAL would
generate noisy tasks that aren't real work.

Actionable markers: TODO, FIXME, HACK, XXX.
Optional: REVIEW (code that needs re-reading, produces review-style tasks).

Usage:
  python tools/liril_markers_to_backlog.py --preview
  python tools/liril_markers_to_backlog.py --commit        # actually add to backlog
  python tools/liril_markers_to_backlog.py --include-critical --commit
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INDEX_DB = Path(r"E:/S.L.A.T.E/tenet5/data/liril_doc_index.sqlite")
BACKLOG  = Path(r"E:/TENET-5.github.io/data/liril_work_schedule.json")

# Action markers — these generate tasks
ACTION_MARKERS = ["TODO", "FIXME", "HACK", "XXX"]
OPT_REVIEW     = "REVIEW"
OPT_CRITICAL   = "CRITICAL"

# Skip these files entirely — markers inside are descriptive, not actionable.
# Each element of SKIP_FILES is a substring test (not regex) so a buggy
# trailing-pipe can't create a match-everything empty alternative.
SKIP_FILES: list[str] = [
    "liril_doc_parser.py",          # this file's own marker description
    "liril_markers_to_backlog.py",  # its own markers
    "liril_failure_analysis.py",    # documents failure categories
    "liril_popup_hunter.py",        # describes marker whitelisting
    "anomaly_detector.py",          # 'Boost if CRITICAL' is scoring logic
    "dashboard_health.py",          # markers describe states, not TODOs
    "circuit_breaker.py",           # best-effort path descriptor
    "/compiler/millennium.py",      # math comments
    "/discoveries/",                # research notes
    "AGENTS.md",                    # the agent contract
    "style-slate.css",              # design-system docs
    "style-slate-motion.css",       # design-system docs
]

def _is_skipped(file_path: str) -> bool:
    return any(s in file_path for s in SKIP_FILES)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_markers(kinds: list[str]) -> list[dict]:
    if not INDEX_DB.exists():
        return []
    conn = sqlite3.connect(str(INDEX_DB))
    rows = conn.execute(
        "SELECT file, line, marker, marker_msg, text FROM doc_chunks "
        "WHERE marker IN (" + ",".join("?" * len(kinds)) + ") "
        "ORDER BY marker, file, line",
        kinds,
    ).fetchall()
    conn.close()
    out = []
    for file, line, marker, msg, text in rows:
        if _is_skipped(file or ""):
            continue
        out.append({
            "file":   file,
            "line":   line,
            "marker": marker,
            "msg":    (msg or "").strip() or (text or "").strip()[:200],
            "text":   (text or "")[:400],
        })
    return out


def marker_to_task(m: dict, seq: int) -> dict:
    """Convert one marker into a backlog task."""
    # Pick a priority: FIXME/XXX high, HACK medium, TODO/REVIEW low
    priority = {
        "FIXME": "high", "XXX": "high",
        "HACK":  "medium",
        "TODO":  "low", "REVIEW": "low", "CRITICAL": "low",
    }.get(m["marker"], "low")

    # Axis — most tooling is TECHNOLOGY
    axis = "TECHNOLOGY"

    # Title
    short_msg = m["msg"][:70].strip().rstrip(".")
    title = f"Resolve {m['marker']} in {Path(m['file']).name}:{m['line']} — {short_msg}"
    title = title[:100]

    # Acceptance: clear + verifiable
    acceptance = (
        f"Inspect the {m['marker']} marker at {m['file']}:{m['line']}. "
        f"Comment body: '{m['msg'][:280]}'. "
        f"Resolve by one of: (1) implementing the TODO / fixing the FIXME / "
        f"removing the HACK, (2) converting the marker to a plain NOTE if the "
        f"behaviour is acceptable, or (3) deleting the comment if obsolete. "
        f"Commit must touch ONLY this one file unless structurally impossible. "
        f"If out-of-scope for one cycle, add a followup comment explaining and "
        f"keep the marker."
    )

    return {
        "id":              f"WS-DOC-{seq:03d}",
        "title":           title,
        "axis_domain":     axis,
        "priority":        priority,
        "role_pipeline":   ["researcher", "architect", "engineer", "editor", "gatekeeper"],
        "target_files":    [m["file"]],
        "acceptance":      acceptance,
        "context":         (
            f"Auto-generated from liril_doc_parser marker index. "
            f"{m['marker']} at line {m['line']}. Source text: "
            f"{m['text'][:240]}"
        ),
        "status":          "pending",
        "created_at":      _utc(),
        "source_marker":   m["marker"],
        "source_file":     m["file"],
        "source_line":     m["line"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL markers → WS-DOC-* backlog tasks")
    ap.add_argument("--preview", action="store_true", help="print what would be added")
    ap.add_argument("--commit",  action="store_true", help="actually append tasks to backlog")
    ap.add_argument("--include-review",   action="store_true", help="also include REVIEW markers")
    ap.add_argument("--include-critical", action="store_true", help="also include CRITICAL markers (noisy)")
    ap.add_argument("--max",     type=int, default=30, help="cap number of tasks (default 30)")
    ap.add_argument("--json",    action="store_true")
    args = ap.parse_args()

    kinds = list(ACTION_MARKERS)
    if args.include_review:   kinds.append(OPT_REVIEW)
    if args.include_critical: kinds.append(OPT_CRITICAL)

    markers = load_markers(kinds)
    if not markers:
        print("no actionable markers found. run liril_doc_parser.py --scan first.")
        return 1

    # Dedup: keep first occurrence per (file, msg) prefix
    seen: set = set()
    deduped: list[dict] = []
    for m in markers:
        key = (m["file"], m["msg"][:80].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    markers = deduped[:args.max]

    # Find next WS-DOC-N sequence number from existing backlog
    seq_start = 1
    if BACKLOG.exists():
        data = json.loads(BACKLOG.read_text(encoding="utf-8"))
        existing_ids = {t.get("id","") for t in data.get("backlog", [])}
        existing_doc_ids = [tid for tid in existing_ids if tid.startswith("WS-DOC-")]
        if existing_doc_ids:
            nums = [int(tid.rsplit("-",1)[1]) for tid in existing_doc_ids if tid.rsplit("-",1)[1].isdigit()]
            if nums:
                seq_start = max(nums) + 1

    new_tasks = [marker_to_task(m, seq_start + i) for i, m in enumerate(markers)]

    if args.preview or not args.commit:
        print(f"── would add {len(new_tasks)} WS-DOC-* tasks ──")
        counts: dict = defaultdict(int)
        for t in new_tasks:
            counts[t["source_marker"]] += 1
        for m, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {m:10s} {n} tasks")
        print()
        for t in new_tasks[:10]:
            print(f"  [{t['id']}] {t['priority']:6s} {t['title'][:85]}")
        if len(new_tasks) > 10:
            print(f"  … and {len(new_tasks)-10} more")
        if args.json:
            print(json.dumps(new_tasks, indent=2, default=str))

    if args.commit:
        data = json.loads(BACKLOG.read_text(encoding="utf-8"))
        data.setdefault("backlog", []).extend(new_tasks)
        data["updated_at"] = _utc()
        BACKLOG.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\n[COMMIT] appended {len(new_tasks)} tasks to {BACKLOG}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
