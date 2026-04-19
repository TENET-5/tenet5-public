# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:40:00Z
"""liril_correct — capture corrections for local AI coder training.

Every time the dev-team produces a role output that turned out wrong
(false-positive PASS, hallucinated citation, empty engineer block, etc.)
you review the transcript and log a correction here. Over time the
accumulated corrections become the fine-tuning dataset per role.

Usage:
  python tools/liril_correct.py list                 # list recent transcripts
  python tools/liril_correct.py show <transcript>    # show one transcript's role outputs
  python tools/liril_correct.py log                   # interactive correction logger
  python tools/liril_correct.py log \\
        --transcript WS-001_1776489500.json \\
        --role gatekeeper \\
        --bad "VERDICT: PASS\\nREASON: ..." \\
        --correct "VERDICT: FAIL\\nREASON: ..." \\
        --annotation "false positive; gatekeeper missed ..." \\
        --severity high \\
        --label negative
  python tools/liril_correct.py stats                 # print per-role counts + metrics
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")
LOG_DIR = SITE / "data" / "liril_dev_team_log"
CORRECTIONS = SITE / "data" / "liril_corrections.jsonl"
REGISTRY = SITE / "data" / "liril_coders.json"

VALID_ROLES = ["researcher", "architect", "designer", "engineer", "editor", "gatekeeper"]
VALID_SEVERITY = ["none", "low", "medium", "high"]
VALID_LABELS = ["positive", "negative", "neutral"]


def load_corrections() -> list[dict]:
    if not CORRECTIONS.exists():
        return []
    out = []
    with CORRECTIONS.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def append_correction(entry: dict) -> None:
    # Append atomically — the JSONL format is designed for this.
    with CORRECTIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_entry_id() -> str:
    existing = load_corrections()
    nums = []
    for e in existing:
        eid = e.get("entry_id", "")
        if eid.startswith("corr-"):
            try:
                nums.append(int(eid.split("-", 1)[1]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"corr-{n:04d}"


def cmd_list(args):
    if not LOG_DIR.exists():
        print(f"(no transcripts yet — {LOG_DIR} missing)")
        return
    files = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("(no transcripts yet)")
        return
    print(f"{'TRANSCRIPT':48s}  {'TASK':10s}  {'STATUS':18s}  {'MOD_UTC':20s}")
    for f in files[:20]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        task_id = d.get("task", {}).get("id", "?")
        status = d.get("status", "?")
        verdict = d.get("verdict", "-")
        mod = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(f.stat().st_mtime))
        print(f"{f.name:48s}  {task_id:10s}  {status:8s}/{verdict:9s}  {mod}")


def cmd_show(args):
    path = LOG_DIR / args.transcript
    if not path.exists():
        print(f"transcript not found: {path}", file=sys.stderr)
        sys.exit(1)
    d = json.loads(path.read_text(encoding="utf-8"))
    task = d.get("task", {})
    print(f"=== {args.transcript} ===")
    print(f"task.id:         {task.get('id')}")
    print(f"task.title:      {task.get('title')}")
    print(f"task.acceptance: {task.get('acceptance')}")
    print(f"pipeline:        {' → '.join(task.get('role_pipeline', []))}")
    print(f"status:          {d.get('status')}  verdict={d.get('verdict')}  reason={d.get('reason')}")
    for r in d.get("roles", []):
        role = r.get("role")
        text = r.get("text", "")
        if r.get("error"):
            print(f"\n--- ROLE: {role}  ERROR ---")
            print(r["error"])
        else:
            print(f"\n--- ROLE: {role}  ({r.get('latency_ms','?')}ms) ---")
            print(text)


def cmd_log(args):
    if args.role not in VALID_ROLES:
        print(f"role must be one of {VALID_ROLES}", file=sys.stderr); sys.exit(1)
    sev = args.severity or "medium"
    if sev not in VALID_SEVERITY:
        print(f"severity must be one of {VALID_SEVERITY}", file=sys.stderr); sys.exit(1)
    label = args.label or ("negative" if sev in ("medium", "high") else "neutral")
    if label not in VALID_LABELS:
        print(f"label must be one of {VALID_LABELS}", file=sys.stderr); sys.exit(1)

    transcript_ref = f"data/liril_dev_team_log/{args.transcript}" if args.transcript else None
    task_id = None
    role_input = args.role_input or ""
    if transcript_ref:
        tp = SITE / transcript_ref
        if tp.exists():
            try:
                d = json.loads(tp.read_text(encoding="utf-8"))
                task_id = d.get("task", {}).get("id")
                if not role_input:
                    # Summarize the upstream context for this role
                    prior = ""
                    for r in d.get("roles", []):
                        if r.get("role") == args.role:
                            break
                        if r.get("text"):
                            prior += f"[{r.get('role')}] {r['text'][:400]}\n"
                    role_input = (
                        f"Task: {d.get('task',{}).get('title','')}. "
                        f"Acceptance: {d.get('task',{}).get('acceptance','')}. "
                        f"Prior-role context: {prior[:800]}"
                    )
            except Exception:
                pass

    entry = {
        "entry_id":       next_entry_id(),
        "timestamp_utc":  int(time.time()),
        "role":           args.role,
        "task_id":        task_id or args.task_id or "",
        "transcript_ref": transcript_ref,
        "role_input":     role_input,
        "bad_output":     args.bad,
        "correct_output": args.correct,
        "annotation":     args.annotation or "",
        "severity":       sev,
        "training_label": label,
    }
    append_correction(entry)
    print(f"logged {entry['entry_id']} for role={args.role} label={label} severity={sev}")


def cmd_stats(args):
    entries = load_corrections()
    print(f"total corrections: {len(entries)}")
    by_role = {}
    by_label = {}
    for e in entries:
        r = e.get("role", "?")
        by_role[r] = by_role.get(r, 0) + 1
        l = e.get("training_label", "?")
        by_label[l] = by_label.get(l, 0) + 1
    print("\nby role:")
    for r in sorted(by_role):
        print(f"  {r:14s} {by_role[r]}")
    print("\nby training_label:")
    for l in sorted(by_label):
        print(f"  {l:10s} {by_label[l]}")
    # Update registry metrics
    if REGISTRY.exists():
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for c in reg.get("coders", []):
            c["training_examples"] = by_role.get(c.get("role"), 0)
        reg["updated_at"] = int(time.time())
        REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nupdated {REGISTRY.name} training_examples counts")


def main():
    ap = argparse.ArgumentParser(description="TENET5 local AI coder correction logger")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("list")

    sh = sp.add_parser("show")
    sh.add_argument("transcript")

    lg = sp.add_parser("log")
    lg.add_argument("--transcript")
    lg.add_argument("--task-id")
    lg.add_argument("--role", required=True)
    lg.add_argument("--role-input")
    lg.add_argument("--bad")
    lg.add_argument("--correct", required=True)
    lg.add_argument("--annotation")
    lg.add_argument("--severity", default="medium")
    lg.add_argument("--label")

    sp.add_parser("stats")

    args = ap.parse_args()
    {
        "list": cmd_list,
        "show": cmd_show,
        "log":  cmd_log,
        "stats": cmd_stats,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
