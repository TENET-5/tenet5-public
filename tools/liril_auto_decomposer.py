#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T04:10:00Z | Author: claude_code | Change: 3-strikes auto-decomposer
"""LIRIL Auto-Decomposer — 3-strikes engineer-empty fixer.

LIRIL picked this as the next-build when offered A/B/C/D:
  'B - Implement a 3-strikes rule for engineer_empty tasks. If a task
   fails twice, auto-decompose it into smaller sub-tasks using
   gpt-compatible heuristics and requeue. This will help reduce task
   failure rates and improve overall efficiency.'

Context: runtime profiler shows engineer at 68.2% schema-ok — the
weakest role. Most failures are 'engineer_empty' on multi-entry tasks
that Mistral-Nemo scope-averts (see the manual-decomposition pattern
we applied to WS-034 → WS-034-{ca,us,uk,fr,de} and WS-033 →
WS-033-001..007). This tool automates that loop.

Algorithm:
  1. Query profiler DB for tasks with ≥ 2 failed engineer calls
     (empty output OR hallucination_caught OR unfenced)
  2. Filter to tasks still in backlog (status in pending/needs_rework)
  3. For each: detect multiplicity cues in the acceptance text
     (every, each, all, array of, map…to, N items, list of N)
  4. Extract the enumeration if possible (country codes, timeline events,
     per-page targets already named in the acceptance)
  5. For each extracted sub-target, build a scoped sub-task with clean
     id = {original}-{slug} and pending status
  6. Mark the original as status = 'decomposed' (new status — recognised
     by the daemon's task picker via the status filter)
  7. Write back to liril_work_schedule.json

Run:
  python tools/liril_auto_decomposer.py --preview     # see what would be decomposed
  python tools/liril_auto_decomposer.py --commit      # actually decompose
  python tools/liril_auto_decomposer.py --daemon      # loop every 10 min
  python tools/liril_auto_decomposer.py --task WS-034 # decompose a specific task

Safety rails:
  - Minimum 2 engineer failures required
  - Maximum 12 sub-tasks per decomposition (cap to avoid runaway)
  - Skip tasks that already have decomposed sub-tasks in the backlog
    (pattern: any id starting with the original id + '-')
  - Dry-run by default
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
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROFILE_DB = Path(r"E:/S.L.A.T.E/tenet5/data/liril_runtime_profile.sqlite")
BACKLOG    = Path(r"E:/TENET-5.github.io/data/liril_work_schedule.json")
NATS_URL   = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ──────────────────────────────────────────────────────────────
# FAILURE COUNTING (from profiler DB)
# ──────────────────────────────────────────────────────────────

def count_engineer_failures() -> dict[str, int]:
    """Return {task_id: n_engineer_failures} across all recorded transcripts."""
    if not PROFILE_DB.exists():
        return {}
    conn = sqlite3.connect(str(PROFILE_DB))
    rows = conn.execute("""
        SELECT task_id, COUNT(*) as fails
        FROM role_calls
        WHERE role = 'engineer'
          AND (
              (text_chars = 0)
              OR had_url_halt = 1
              OR status IN ('schema_validation_failed', 'hallucination_caught', 'error_during_role')
              OR schema_ok = 0
          )
        GROUP BY task_id
        HAVING fails >= 2
        ORDER BY fails DESC
    """).fetchall()
    conn.close()
    return {tid: n for tid, n in rows}


# ──────────────────────────────────────────────────────────────
# MULTIPLICITY DETECTION + EXTRACTION
# ──────────────────────────────────────────────────────────────

_MULTIPLICITY_RX = re.compile(
    r"\b(every|each|all\s+\w+|array\s+of|list\s+of|map(?:ping)?\s+.*\s+to|per[-\s]\w+)\b",
    re.I,
)

# Common enumerations we can extract
_COUNTRY_CODE_RX = re.compile(r"\(?\b(CA|US|USA|UK|GB|FR|DE|AU|NZ|IT|ES|JP|CN|IN)\b\)?", re.I)
_YEAR_LIST_RX   = re.compile(r"\b(19|20)\d{2}\b")
_NUM_ITEMS_RX   = re.compile(r"\b(?:at\s+minimum|min(?:imum)?)\s*(\d{1,2})\s*(?:entries|items|records)\b", re.I)

# Known file enumerations (from real backlog tasks)
_HTML_FILE_RX = re.compile(r"\b([\w-]+\.html)\b")


def detect_multiplicity(acceptance: str) -> dict:
    """Look at task acceptance text; return what kind of decomposition to do."""
    if not _MULTIPLICITY_RX.search(acceptance):
        return {"detected": False}

    # Look for country codes
    countries = list({m.group(1).upper() for m in _COUNTRY_CODE_RX.finditer(acceptance)})
    if len(countries) >= 3:
        return {"detected": True, "kind": "countries", "enum": countries}

    # Look for HTML file lists
    html_files = list({m.group(1) for m in _HTML_FILE_RX.finditer(acceptance)})
    if len(html_files) >= 3:
        return {"detected": True, "kind": "html_files", "enum": html_files[:12]}

    # Look for "at minimum N entries" hint
    m = _NUM_ITEMS_RX.search(acceptance)
    if m:
        n = min(int(m.group(1)), 12)
        return {"detected": True, "kind": "numeric_items", "enum": list(range(1, n + 1))}

    return {"detected": True, "kind": "unknown", "enum": []}


# ──────────────────────────────────────────────────────────────
# SUB-TASK GENERATION
# ──────────────────────────────────────────────────────────────

def _slug(val) -> str:
    s = str(val).lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9\-]", "", s)[:20]


def build_subtasks(task: dict, decomposition: dict) -> list[dict]:
    """Build concrete sub-task records from a detected decomposition."""
    if not decomposition.get("enum"):
        return []
    now = _utc()
    base_id = task["id"]
    base_accept = task.get("acceptance", "")
    base_pipeline = task.get("role_pipeline") or [
        "researcher", "architect", "engineer", "editor", "gatekeeper"
    ]
    base_targets = task.get("target_files") or []

    out: list[dict] = []
    kind = decomposition["kind"]
    for item in decomposition["enum"]:
        slug = _slug(item)
        if kind == "countries":
            sub_accept = (
                f"Add the single entry for {item} to whichever top-level "
                f"container the parent task {base_id} specifies. Emit a "
                f"complete JSON object like {{'<container>': {{'{item}': "
                f"{{...fields...}}}}}} so the applier sub-merges the key. "
                f"Parent acceptance (for reference): {base_accept[:300]}"
            )
            title = f"{item} entry from decomposed {base_id}"
        elif kind == "html_files":
            sub_accept = (
                f"Apply the parent task change to ONLY the file {item}. "
                f"Do not touch any other file. Parent acceptance: "
                f"{base_accept[:300]}"
            )
            title = f"{item} scope from decomposed {base_id}"
            base_targets = [item]
        elif kind == "numeric_items":
            sub_accept = (
                f"Append ONE entry (number {item} of the expected set) "
                f"to the parent array. Entry shape follows parent task "
                f"{base_id}. Parent acceptance: {base_accept[:300]}"
            )
            title = f"entry #{item:02d} from decomposed {base_id}"
        else:
            continue

        out.append({
            "id":             f"{base_id}-{slug}"[:60],
            "title":          title[:100],
            "axis_domain":    task.get("axis_domain") or "TECHNOLOGY",
            "priority":       "high",
            "role_pipeline":  base_pipeline,
            "target_files":   base_targets,
            "acceptance":     sub_accept,
            "context":        f"Auto-decomposed from {base_id} after 2+ engineer failures.",
            "status":         "pending",
            "created_at":     now,
            "decomposed_from": base_id,
        })
    return out


# ──────────────────────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────────────────────

def already_decomposed(backlog_data: dict, task_id: str) -> bool:
    """Skip tasks that already have child sub-tasks in the backlog."""
    for t in backlog_data.get("backlog", []):
        tid = t.get("id", "")
        if tid != task_id and tid.startswith(task_id + "-"):
            return True
    return False


def process(preview: bool = True, min_strikes: int = 2, task_filter: str | None = None) -> dict:
    if not BACKLOG.exists():
        return {"error": f"backlog not found: {BACKLOG}"}

    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    task_by_id = {t.get("id"): t for t in data.get("backlog", [])}
    failures = count_engineer_failures()

    candidates = []
    for tid, fails in failures.items():
        if task_filter and tid != task_filter:
            continue
        if fails < min_strikes:
            continue
        task = task_by_id.get(tid)
        if not task:
            continue  # task no longer in backlog (already done or removed)
        if task.get("status") in ("done", "decomposed"):
            continue
        if already_decomposed(data, tid):
            continue
        candidates.append((task, fails))

    report = {
        "candidates":        len(candidates),
        "decomposed":        0,
        "skipped_no_enum":   0,
        "sub_tasks_added":   0,
        "tasks":             [],
    }

    for task, fails in candidates:
        tid = task["id"]
        accept = task.get("acceptance", "")
        decomposition = detect_multiplicity(accept)
        entry = {
            "task":     tid,
            "fails":    fails,
            "detected": decomposition.get("detected"),
            "kind":     decomposition.get("kind"),
            "enum_n":   len(decomposition.get("enum") or []),
        }
        if not decomposition.get("detected") or not decomposition.get("enum"):
            report["skipped_no_enum"] += 1
            entry["action"] = "skip_no_enum"
            report["tasks"].append(entry)
            continue

        subs = build_subtasks(task, decomposition)
        if not subs:
            report["skipped_no_enum"] += 1
            entry["action"] = "skip_no_subs"
            report["tasks"].append(entry)
            continue

        entry["action"] = "decompose"
        entry["sub_ids"] = [s["id"] for s in subs]

        if not preview:
            # Mark original as decomposed
            task["status"] = "decomposed"
            task["decomposed_at"] = _utc()
            task["decomposed_into"] = [s["id"] for s in subs]
            # Append sub-tasks
            data["backlog"].extend(subs)
            report["sub_tasks_added"] += len(subs)
            report["decomposed"] += 1

        report["tasks"].append(entry)

    if not preview and report["decomposed"] > 0:
        data["updated_at"] = _utc()
        BACKLOG.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return report


# ──────────────────────────────────────────────────────────────
# NATS DAEMON
# ──────────────────────────────────────────────────────────────

async def daemon(interval: int = 600) -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[AUTO-DECOMPOSER] daemon every {interval}s, NATS {NATS_URL}")

    async def publish_report(report: dict):
        try:
            await nc.publish(
                "tenet5.liril.decompose.report",
                json.dumps(report, default=str).encode(),
            )
        except Exception:
            pass

    while True:
        try:
            r = process(preview=False)
            print(f"[AUTO-DECOMPOSER] candidates={r['candidates']} "
                  f"decomposed={r['decomposed']} subs_added={r['sub_tasks_added']}")
            await publish_report(r)
        except Exception as e:
            print(f"[AUTO-DECOMPOSER] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL 3-strikes auto-decomposer")
    ap.add_argument("--preview", action="store_true", help="dry-run (default)")
    ap.add_argument("--commit",  action="store_true", help="actually apply decompositions")
    ap.add_argument("--task",    type=str, default=None, help="target a specific task id")
    ap.add_argument("--strikes", type=int, default=2, help="min engineer failures (default 2)")
    ap.add_argument("--daemon",  action="store_true", help="loop every 10 min")
    ap.add_argument("--json",    action="store_true")
    args = ap.parse_args()

    if args.daemon:
        asyncio.run(daemon())
        return 0

    preview = not args.commit
    r = process(preview=preview, min_strikes=args.strikes, task_filter=args.task)

    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        mode = "PREVIEW" if preview else "COMMIT"
        print(f"── auto-decomposer [{mode}] ──")
        print(f"  candidates:      {r.get('candidates', 0)}")
        print(f"  decomposed:      {r.get('decomposed', 0)}")
        print(f"  skipped_no_enum: {r.get('skipped_no_enum', 0)}")
        print(f"  sub_tasks_added: {r.get('sub_tasks_added', 0)}")
        for t in r.get("tasks", [])[:20]:
            action = t.get("action", "?")
            print(f"  [{t['fails']} strikes] {t['task']:12s}  "
                  f"{t.get('kind') or 'no-enum':15s}  "
                  f"n={t.get('enum_n', 0)}  → {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
