# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:30:00Z
# SYSTEM_SEED=118400
"""Site auditor for the LIRIL autonomous dev daemon.

PURE PYTHON, no LLM. Detects improvement opportunities in the committed site:
  1. Narration integrity issues (TOO_SHORT / TOO_LONG_SOFT / EMPTY)
  2. Broken internal links (site-relative href/src pointing nowhere)
  3. Axis cross-references missing (a dossier JSON exists but its actors are
     not linked from related pages)
  4. Merkle drift (data file hash differs from last receipt for that file)

Returns a prioritized task list as JSON. The daemon then picks one and
dispatches to LIRIL for drafting.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")


def check_narration_integrity() -> list[dict]:
    """Find pages with narration-length issues worth filling."""
    issues = []
    NARRATE_RE = re.compile(r'data-narrate\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.IGNORECASE | re.DOTALL)
    for p in sorted(SITE.glob("*.html")):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, m in enumerate(NARRATE_RE.finditer(txt)):
            n = (m.group(1) or m.group(2) or "").strip()
            wc = len(n.split())
            if wc == 0:
                issues.append({"type": "narration_empty", "page": p.name, "block_idx": i, "severity": "high"})
            elif wc < 5:
                issues.append({"type": "narration_too_short", "page": p.name, "block_idx": i, "words": wc, "severity": "low"})
            elif len(n) > 2400:
                issues.append({"type": "narration_too_long_hard", "page": p.name, "block_idx": i, "chars": len(n), "severity": "high"})
    return issues


def check_broken_links() -> list[dict]:
    """Find site-relative href/src that point to non-existent files."""
    issues = []
    LINK_RE = re.compile(
        r'(?:href|src)\s*=\s*["\'](?!https?://)(?!#)(?!data:)(?!mailto:)(?!javascript:)([^"\'#?\s]+)(?:[#?\s])?',
        re.IGNORECASE,
    )
    existing = set(p.relative_to(SITE).as_posix() for p in SITE.rglob("*"))
    for p in sorted(SITE.glob("*.html")):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in LINK_RE.finditer(txt):
            link = m.group(1).strip().lstrip("/")
            link_norm = link.split("#")[0].split("?")[0]
            if not link_norm:
                continue
            # Skip JS template literals (${...}) and regex backreferences ($1, $2, etc.)
            if "${" in link_norm or link_norm.startswith("$") or "}" in link_norm:
                continue
            # Skip anything that looks like a dynamic expression
            if any(ch in link_norm for ch in ("(", ")", " + ", "\\")):
                continue
            if link_norm not in existing and not (SITE / link_norm / "index.html").exists():
                issues.append({"type": "broken_link", "page": p.name, "target": link_norm, "severity": "high"})
    return issues


def check_axis_cross_refs() -> list[dict]:
    """Find Grover-marked actors whose name is not hyperlinked anywhere
    across the site's content pages. These are candidates for backlinks."""
    issues = []
    actors = set()
    for dossier in SITE.glob("data/*_grover_decisionmakers.json"):
        try:
            d = json.loads(dossier.read_text(encoding="utf-8"))
        except Exception:
            continue
        marked = d.get("marked_actors", [])
        for a in marked:
            if isinstance(a, dict):
                n = a.get("name")
                if n:
                    actors.add(n)
            elif isinstance(a, str):
                actors.add(a)
    # For each actor, count appearances in HTML pages (as plain text)
    actor_counts = {a: 0 for a in actors}
    for p in sorted(SITE.glob("*.html")):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for a in actors:
            if a in txt:
                actor_counts[a] += 1
    for actor, count in actor_counts.items():
        if count == 0:
            issues.append({"type": "actor_not_referenced", "actor": actor, "severity": "low"})
        elif count == 1:
            issues.append({"type": "actor_sparse_reference", "actor": actor, "pages": count, "severity": "info"})
    return issues


def check_merkle_drift() -> list[dict]:
    """Compare current data file SHA-256 against last-published receipt."""
    issues = []
    # We rely on the data/*_merkle.json receipts for prior hashes
    merkle_files = list(SITE.glob("data/*_merkle.json"))
    for mf in merkle_files:
        try:
            receipt = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        files = receipt.get("files", {})
        for fname, finfo in files.items():
            if not isinstance(finfo, dict):
                continue
            expected_sha = finfo.get("sha256")
            if not expected_sha:
                continue
            # Resolve file location heuristically
            candidates = [SITE / fname, SITE / "data" / fname, SITE / "data" / "campaigns_to_send" / fname]
            actual_sha = None
            for c in candidates:
                if c.is_file():
                    h = hashlib.sha256()
                    with c.open("rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    actual_sha = h.hexdigest()
                    break
            if actual_sha and actual_sha != expected_sha:
                issues.append({
                    "type": "merkle_drift",
                    "file": fname,
                    "expected": expected_sha[:16] + "...",
                    "actual": actual_sha[:16] + "...",
                    "receipt": mf.name,
                    "severity": "high",
                })
    return issues


def prioritize(all_issues: list[dict]) -> list[dict]:
    """Sort by severity: high > low > info. Keep top 20."""
    sev_rank = {"high": 0, "low": 1, "info": 2}
    return sorted(all_issues, key=lambda i: (sev_rank.get(i.get("severity"), 3), i.get("type", "")))


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL autonomous site auditor")
    ap.add_argument("--write-report", default=None, help="Path to write JSON report")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary to stdout")
    args = ap.parse_args()

    all_issues: list[dict] = []
    if True:
        all_issues.extend(check_narration_integrity())
    if True:
        all_issues.extend(check_broken_links())
    if True:
        all_issues.extend(check_axis_cross_refs())
    if True:
        all_issues.extend(check_merkle_drift())
    prioritized = prioritize(all_issues)

    summary = {
        "seed": 118400,
        "total_issues": len(prioritized),
        "by_type": {},
        "by_severity": {"high": 0, "low": 0, "info": 0},
        "top_20": prioritized[:20],
    }
    for i in prioritized:
        t = i.get("type", "?")
        summary["by_type"][t] = summary["by_type"].get(t, 0) + 1
        sev = i.get("severity", "?")
        if sev in summary["by_severity"]:
            summary["by_severity"][sev] += 1

    out = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.write_report:
        Path(args.write_report).write_text(out, encoding="utf-8")

    if args.json:
        print(out)
    else:
        print("═" * 70)
        print(f"LIRIL Site Audit · SEED {summary['seed']}")
        print("═" * 70)
        print(f"  total issues: {summary['total_issues']}")
        print(f"  by severity: {summary['by_severity']}")
        print(f"  by type: {summary['by_type']}")
        print()
        print("  TOP 15 PRIORITIZED ISSUES:")
        for i in prioritized[:15]:
            type_s = i.get("type", "?")
            sev = i.get("severity", "?")
            rest = {k: v for k, v in i.items() if k not in ("type", "severity")}
            rest_s = " ".join(f"{k}={v}" for k, v in list(rest.items())[:2])
            print(f"    [{sev:5s}] {type_s:30s} {rest_s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
