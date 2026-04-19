#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T21:40:00Z | Author: claude_code | Change: failure-analysis agent
"""LIRIL Learning-from-Failure analyzer (LIRIL agentic enhancement #5).

LIRIL's own self-request via mercury.infer.code (2026-04-18 evening):
'Incorporate a learning-from-failure mechanism that identifies and
learns from instances where LIRIL's actions or decisions led to
undesirable outcomes.'

This agent reads dev_team_log/*.json transcripts, classifies each
into success / failure categories by specific mode, aggregates the
failure patterns, and publishes findings to
tenet5.liril.failure_analysis so downstream components can adapt.

Run:
  python tools/liril_failure_analysis.py           # one-shot report
  python tools/liril_failure_analysis.py --publish # also publish to NATS
  python tools/liril_failure_analysis.py --recent 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
TRANSCRIPT_DIR = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")


def _classify_cycle(entry: dict) -> tuple[str, dict]:
    status = (entry.get("status") or "").lower()
    evidence = {"task_id": entry.get("task", {}).get("id", "?"),
                "axis": (entry.get("task", {}).get("axis_domain") or "").upper()}
    if status == "hallucination_caught":
        evidence["dead_urls"] = entry.get("dead_urls", [])[:2]
        return "hallucination_caught", evidence
    if status == "editor_requested_rework":
        return "editor_requested_rework", evidence
    if status == "schema_validation_failed":
        failed = entry.get("failed_role", "?")
        issues = entry.get("schema_issues", [])
        evidence["failed_role"] = failed
        evidence["schema_issues"] = issues[:2]
        issue_text = " ".join(str(i) for i in issues).lower()
        if failed == "engineer":
            if "empty output" in issue_text:
                return "engineer_empty", evidence
            if "fenced code block" in issue_text:
                return "engineer_unfenced", evidence
        if failed == "researcher":
            if "missing field" in issue_text:
                return "researcher_missing", evidence
        return f"schema_fail_{failed}", evidence
    if status == "gated":
        verdict = (entry.get("verdict") or "").upper()
        if verdict == "PASS":
            return "success", evidence
        if verdict == "FAIL":
            return "gatekeeper_fail", evidence
        if verdict == "WATCH":
            return "gatekeeper_watch", evidence
        if verdict == "UNPARSED":
            return "gatekeeper_unparsed", evidence
        return f"gated_{verdict}", evidence
    if status == "error_during_role":
        evidence["error_role"] = entry.get("error_role", "?")
        return "error_during_role", evidence
    return f"unknown_{status or 'empty'}", evidence


def analyze(limit: int | None = None) -> dict:
    if not TRANSCRIPT_DIR.exists():
        return {"error": f"no dir: {TRANSCRIPT_DIR}"}
    files = sorted(TRANSCRIPT_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if limit:
        files = files[:limit]
    category_counts = Counter()
    per_axis: dict[str, Counter] = defaultdict(Counter)
    per_failed_role: Counter = Counter()
    per_issue: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    for fp in files:
        try:
            entry = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        cat, evidence = _classify_cycle(entry)
        category_counts[cat] += 1
        axis = evidence.get("axis", "?")
        per_axis[axis][cat] += 1
        if evidence.get("failed_role"):
            per_failed_role[evidence["failed_role"]] += 1
        for i in evidence.get("schema_issues", []):
            per_issue[str(i)[:120]] += 1
        if len(examples[cat]) < 3:
            examples[cat].append(evidence)
    total = sum(category_counts.values())
    failure_cats = [c for c in category_counts if c != "success"]
    failures = sum(category_counts[c] for c in failure_cats)
    pass_rate = (category_counts.get("success", 0) / total) if total else 0.0
    return {
        "total_cycles":      total,
        "success_count":     category_counts.get("success", 0),
        "failure_count":     failures,
        "pass_rate":         round(pass_rate, 3),
        "by_category":       dict(category_counts.most_common()),
        "by_axis":           {axis: dict(c.most_common()) for axis, c in per_axis.items()},
        "by_failed_role":    dict(per_failed_role.most_common()),
        "top_schema_issues": dict(per_issue.most_common(10)),
        "examples":          dict(examples),
        "recommendations":   _recommend(category_counts, per_failed_role, per_issue),
    }


def _recommend(cat: Counter, roles: Counter, issues: Counter) -> list[str]:
    recs = []
    total = sum(cat.values()) or 1
    if cat.get("engineer_empty", 0) / total > 0.15:
        # 2026-04-18 update: the original hypothesis (prompt bloat) was
        # DISPROVEN by direct llama-server probe. Real root cause was
        # nemo_server._handle_code_infer wrapping prompts in a hardcoded
        # 'expert python developer' system message, contradicting TENET5
        # roles. Fix landed in commit 2b50a5ab6 (send messages directly).
        # If empty rate is still >15% post-fix, the pattern is usually
        # task-output-size: tasks that need ~2000+ char outputs (multi-
        # item arrays, multi-file edits, entire new files) trigger
        # Mistral-Nemo scope-aversion — decompose into smaller subtasks.
        recs.append("engineer_empty >15% — verify liril_dev_team sends "
                    "messages= (not prompt=) to bypass nemo_server's "
                    "default code-wrapper; residual cases are usually "
                    "large-output tasks that need backlog decomposition")
    if cat.get("engineer_unfenced", 0) / total > 0.10:
        recs.append("engineer_unfenced >10% — reinforce 'wrap in ```...```' "
                    "in engineer prompt; also verify max_tokens budget is "
                    "generous enough that model doesn't truncate the fence")
    if cat.get("researcher_missing", 0) / total > 0.15:
        recs.append("researcher_missing >15% — too strict SOURCE/QUOTE/GAP_FILLED requirement; "
                    "consider allowing 'internal change' for schema-only tasks")
    if cat.get("gatekeeper_unparsed", 0) / total > 0.10:
        recs.append("gatekeeper_unparsed >10% — fallback parser may need another tier")
    if cat.get("hallucination_caught", 0) > 0:
        recs.append(f"hallucination_caught happened {cat['hallucination_caught']} times — "
                    "URL gate is working; consider adding verified-URL bank")
    if roles.get("engineer", 0) > roles.get("researcher", 0) + roles.get("gatekeeper", 0):
        recs.append(f"engineer is the most common failure role ({roles['engineer']}) — "
                    "highest-leverage optimization target")
    if not recs:
        recs.append("no single category > 15% — failure is distributed; "
                    "next optimization is axis-specific")
    return recs


async def _publish(findings: dict) -> str:
    try:
        import nats
    except ImportError:
        return "nats-py missing"
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        return f"nats connect failed: {e!r}"
    try:
        summary = {k: v for k, v in findings.items() if k != "examples"}
        await nc.publish("tenet5.liril.failure_analysis",
                         json.dumps(summary).encode())
        await nc.flush(timeout=5)
    finally:
        await nc.drain()
    return "published"


def main():
    ap = argparse.ArgumentParser(description="LIRIL failure-analysis agent")
    ap.add_argument("--recent", type=int, default=None,
                    help="only analyze the N most-recent transcripts")
    ap.add_argument("--publish", action="store_true",
                    help="also publish summary to tenet5.liril.failure_analysis")
    ap.add_argument("--json", action="store_true",
                    help="emit full results as JSON")
    args = ap.parse_args()
    findings = analyze(limit=args.recent)
    if "error" in findings:
        print(f"ERROR: {findings['error']}")
        return 1
    if args.json:
        print(json.dumps(findings, indent=2, default=str))
    else:
        print(f"── LIRIL failure analysis ({findings['total_cycles']} cycles) ──")
        print(f"  Pass rate:    {findings['pass_rate']:.1%}")
        print(f"  Successes:    {findings['success_count']}")
        print(f"  Failures:     {findings['failure_count']}")
        print()
        print("  Top categories:")
        for cat, n in list(findings["by_category"].items())[:10]:
            pct = 100 * n / findings["total_cycles"]
            print(f"    {cat:30s} {n:4d}  ({pct:5.1f}%)")
        if findings["by_failed_role"]:
            print("\n  Failures by role:")
            for role, n in findings["by_failed_role"].items():
                print(f"    {role:15s} {n}")
        if findings["top_schema_issues"]:
            print("\n  Top schema issues:")
            for issue, n in list(findings["top_schema_issues"].items())[:5]:
                print(f"    [{n:3d}]  {issue}")
        print("\n  Recommendations:")
        for r in findings["recommendations"]:
            print(f"    * {r}")
    if args.publish:
        r = asyncio.run(_publish(findings))
        print(f"\n  Publish: {r}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
