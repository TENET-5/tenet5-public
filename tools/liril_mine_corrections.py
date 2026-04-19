# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:10:00Z
"""Heuristic correction proposer.

Scans transcripts in data/liril_dev_team_log/ and flags role outputs
that are LIKELY wrong, using pattern rules. Each flagged output becomes
a proposed correction draft the reviewer can accept, reject, or edit
before appending to data/liril_corrections.jsonl.

The goal is force-multiplication: when the daemon runs 50 cycles a day
and produces 300 role outputs, the reviewer cannot read every one. This
tool surfaces the 10-20 most suspicious outputs per review session.

Heuristic rules (v1 — intentionally conservative, low false-positive rate):
  researcher:
    - SOURCE contains "general knowledge" / "common knowledge" / "widely known"
    - QUOTE uses "allegedly" or other hedging when the source is supposedly primary
    - SOURCE claims a page/paragraph that doesn't match the target filename pattern
  engineer:
    - Output has no fenced code block
    - Fenced block marked as json but content doesn't parse
    - Fenced block larger than 8000 bytes (over-engineered single output)
  editor:
    - OVERALL: CLEAN emitted when CHECK_CITATIONS failed
    - Missing any of the three CHECK_ fields
  gatekeeper:
    - VERDICT: PASS when the task's acceptance says "add/write/update/create"
      but no engineer role was in the pipeline OR the engineer output was empty
    - VERDICT value not in {PASS, WATCH, FAIL}

Usage:
  python tools/liril_mine_corrections.py                   # scan all transcripts, print proposals
  python tools/liril_mine_corrections.py --since 24h        # only recent transcripts
  python tools/liril_mine_corrections.py --write-drafts     # write proposals to data/liril_correction_drafts.jsonl
  python tools/liril_mine_corrections.py --accept-all       # auto-append every proposal to corrections.jsonl (DANGEROUS — review first)
"""
from __future__ import annotations
import argparse
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")
LOG_DIR = SITE / "data" / "liril_dev_team_log"
DRAFTS = SITE / "data" / "liril_correction_drafts.jsonl"
CORRECTIONS = SITE / "data" / "liril_corrections.jsonl"

ACTION_VERBS = ["add", "write", "update", "create", "insert", "append", "extend"]


def parse_since(arg: str | None) -> float | None:
    if not arg:
        return None
    m = re.match(r"^(\d+)([hd])$", arg.strip().lower())
    if not m:
        raise ValueError(f"--since must look like '24h' or '7d', got {arg!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
    return (datetime.utcnow() - delta).timestamp()


def extract_section(text: str, label: str) -> str | None:
    if not text:
        return None
    m = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.+?)(?:\n\s*[A-Z_]+\s*:|$)", text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else None


def rules_researcher(task: dict, text: str) -> list[str]:
    flags = []
    src = (extract_section(text, "SOURCE") or "").lower()
    if any(g in src for g in ["general knowledge", "common knowledge", "widely known", "it is known"]):
        flags.append("SOURCE uses vague/non-primary phrasing")
    quote = (extract_section(text, "QUOTE") or "").lower()
    if "allegedly" in quote or "reportedly" in quote:
        flags.append("QUOTE uses hedging language — primary sources should quote directly")
    # Simple fabrication heuristic: SOURCE names an AG/PBO/Hansard report but
    # the task has nothing to do with that subject domain.
    task_text = (task.get("title","") + " " + task.get("acceptance","") + " " + task.get("context","")).lower()
    if "ag 2024" in src or "ag report" in src:
        if not any(k in task_text for k in ["audit", "ag ", "auditor", "arrivecan", "phoenix", "mckinsey", "covid"]):
            flags.append("SOURCE cites AG report but task domain doesn't match any AG-reviewed subject — possible fabricated citation")
    return flags


def rules_engineer(text: str, target_ext: str | None) -> list[str]:
    flags = []
    fence = re.search(r"```(\w*)\n([\s\S]*?)```", text or "")
    if not fence:
        flags.append("no fenced code block in engineer output")
        return flags
    lang = (fence.group(1) or "").strip().lower()
    body = fence.group(2)
    if not body.strip():
        flags.append("fenced code block is empty")
        return flags
    if len(body) > 8000:
        flags.append(f"fenced block unusually large ({len(body)} bytes) — engineers should produce one focused change")
    if lang in ("json", "jsonc") or (target_ext == ".json" and not lang):
        try:
            json.loads(body)
        except Exception as e:
            flags.append(f"engineer body declared/expected JSON but fails to parse: {e}")
    return flags


def rules_editor(text: str) -> list[str]:
    flags = []
    for label in ("CHECK_CITATIONS", "CHECK_LINKS", "CHECK_A11Y", "OVERALL"):
        if not extract_section(text or "", label):
            flags.append(f"missing required field {label}")
    cite = (extract_section(text or "", "CHECK_CITATIONS") or "").lower()
    overall = (extract_section(text or "", "OVERALL") or "").lower()
    if ("missing" in cite or "fail" in cite or "hallucinat" in cite) and "clean" in overall:
        flags.append("OVERALL: CLEAN is inconsistent with CHECK_CITATIONS flagging a problem — editor contradicted itself")
    return flags


def rules_gatekeeper(task: dict, gk_text: str, prior_roles: list[dict]) -> list[str]:
    flags = []
    verdict_line = extract_section(gk_text or "", "VERDICT") or ""
    verdict = verdict_line.strip().split()[0].upper().strip(".,;:") if verdict_line.strip() else ""
    if verdict not in ("PASS", "WATCH", "FAIL"):
        flags.append(f"VERDICT value out of spec: got {verdict_line!r}")
        return flags
    if verdict != "PASS":
        return flags  # only PASS is risky for false-positives
    # PASS specifically — check whether the acceptance required action + we actually did it
    acc = (task.get("acceptance","") + " " + task.get("title","")).lower()
    if not any(v in acc for v in ACTION_VERBS):
        return flags  # audit-only task, PASS is defensible
    # Action-verb task: require an engineer output that has a fenced block
    engineer = next((r for r in prior_roles if r.get("role") == "engineer"), None)
    if engineer is None:
        flags.append("PASS on action-verb task with NO engineer role in pipeline — likely false positive")
        return flags
    eng_text = engineer.get("text", "") or ""
    if not re.search(r"```", eng_text):
        flags.append("PASS on action-verb task but engineer emitted no fenced code block — likely false positive")
    elif re.search(r"```\s*\n\s*```", eng_text) or not eng_text.strip():
        flags.append("PASS on action-verb task but engineer output appears empty or near-empty")
    return flags


ROLE_RULES = {
    "researcher": lambda task, role_out, prior_roles: rules_researcher(task, role_out.get("text","")),
    "engineer":   lambda task, role_out, prior_roles: rules_engineer(role_out.get("text",""), Path(task.get("target_files",[""])[0]).suffix if task.get("target_files") else None),
    "editor":     lambda task, role_out, prior_roles: rules_editor(role_out.get("text","")),
    "gatekeeper": lambda task, role_out, prior_roles: rules_gatekeeper(task, role_out.get("text",""), prior_roles),
}


def scan_transcript(path: Path) -> list[dict]:
    """Return list of proposed correction drafts for this transcript."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    task = d.get("task", {})
    roles = d.get("roles", [])
    proposals = []
    for i, r in enumerate(roles):
        role = r.get("role")
        rules = ROLE_RULES.get(role)
        if rules is None:
            continue
        flags = rules(task, r, roles[:i])
        if flags:
            proposals.append({
                "entry_id":       None,  # assigned at write time
                "proposed_at":    int(time.time()),
                "role":           role,
                "task_id":        task.get("id"),
                "transcript_ref": f"data/liril_dev_team_log/{path.name}",
                "role_input":     (
                    f"Task: {task.get('title','')}. "
                    f"Acceptance: {task.get('acceptance','')}. "
                    f"Prior roles: {[pr.get('role') for pr in roles[:i]]}"
                )[:1000],
                "bad_output":     r.get("text",""),
                "correct_output_placeholder": (
                    "(reviewer: edit this with the correct output before accepting)"
                ),
                "heuristic_flags": flags,
                "severity":        "high" if len(flags) > 1 else "medium",
                "training_label":  "negative",
            })
    return proposals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only scan transcripts modified within this window, e.g. 24h or 7d")
    ap.add_argument("--write-drafts", action="store_true", help="write proposals to data/liril_correction_drafts.jsonl")
    ap.add_argument("--accept-all", action="store_true", help="auto-append all proposals to data/liril_corrections.jsonl (USE WITH CAUTION)")
    args = ap.parse_args()

    since_ts = parse_since(args.since)

    if not LOG_DIR.exists():
        print(f"(no transcripts at {LOG_DIR})")
        return

    files = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if since_ts is not None:
        files = [f for f in files if f.stat().st_mtime >= since_ts]

    all_proposals = []
    for f in files:
        all_proposals.extend(scan_transcript(f))

    print(f"scanned {len(files)} transcript(s); {len(all_proposals)} proposal(s)")
    by_role = {}
    for p in all_proposals:
        by_role[p["role"]] = by_role.get(p["role"], 0) + 1
    for r in sorted(by_role):
        print(f"  {r:12s}  {by_role[r]} proposals")

    if args.write_drafts and all_proposals:
        with DRAFTS.open("a", encoding="utf-8") as f:
            for p in all_proposals:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"wrote {len(all_proposals)} drafts to {DRAFTS}")

    if args.accept_all and all_proposals:
        # Assign entry_ids and copy into the real corrections ledger.
        existing = []
        if CORRECTIONS.exists():
            for L in CORRECTIONS.read_text(encoding="utf-8").splitlines():
                if L.strip():
                    try: existing.append(json.loads(L))
                    except Exception: pass
        nums = [int(e["entry_id"].split("-",1)[1]) for e in existing if e.get("entry_id","").startswith("corr-")]
        next_num = (max(nums)+1) if nums else 1
        with CORRECTIONS.open("a", encoding="utf-8") as f:
            for p in all_proposals:
                p["entry_id"] = f"corr-{next_num:04d}"
                p["timestamp_utc"] = int(time.time())
                p["correct_output"] = (
                    "(auto-accepted from heuristic proposal — reviewer should re-open and set this)"
                )
                p["annotation"] = f"heuristic flags: {p['heuristic_flags']}"
                # write only the canonical fields
                entry = {
                    "entry_id":       p["entry_id"],
                    "timestamp_utc":  p["timestamp_utc"],
                    "role":           p["role"],
                    "task_id":        p["task_id"],
                    "transcript_ref": p["transcript_ref"],
                    "role_input":     p["role_input"],
                    "bad_output":     p["bad_output"],
                    "correct_output": p["correct_output"],
                    "annotation":     p["annotation"],
                    "severity":       p["severity"],
                    "training_label": p["training_label"],
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                next_num += 1
        print(f"auto-accepted {len(all_proposals)} proposals into {CORRECTIONS}")

    # Always print the first 5 proposals so the operator can eyeball
    for p in all_proposals[:5]:
        print()
        print(f"── {p['role']:12s}  task={p['task_id']}  transcript={Path(p['transcript_ref']).name}")
        for fl in p["heuristic_flags"]:
            print(f"     ⚠ {fl}")


if __name__ == "__main__":
    main()
