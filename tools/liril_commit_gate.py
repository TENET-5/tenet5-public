# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:45:00Z
# SYSTEM_SEED=118400
"""Pre-commit gate for LIRIL autonomous dev daemon.

Before any autonomous commit lands on the liril-auto branch, this gate must
pass. Checks:

  1. validate_liril_integration.py — structural CI (12/12 guards)
  2. scan_narration_integrity.py — no hard failures (empty / >2400-char blocks)
  3. Banned-path check — nothing in the never_touch_paths list was modified
  4. Size-limit check — new chars ≤ max, files ≤ max
  5. Hallucination probe — if draft came from LIRIL, run fact probes

Returns JSON: { "ok": bool, "failures": [...], "warnings": [...] }
Exit 0 = pass · Exit 1 = block.

Usage:
  python liril_commit_gate.py --staged   # check git-staged changes
  python liril_commit_gate.py --draft-json path/to/draft.json
"""
from __future__ import annotations
import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")
CONFIG = SITE / "data" / "liril_autonomous_config.json"


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


def check_ci() -> tuple[bool, list[str]]:
    """Run validate_liril_integration.py; return (ok, error_lines)."""
    try:
        r = subprocess.run(
            [str(SITE.parent / "S.L.A.T.E" / ".venv" / "Scripts" / "python.exe"),
             str(SITE / "scripts" / "validate_liril_integration.py")],
            cwd=str(SITE), capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            return (True, [])
        errors = [line for line in (r.stdout + r.stderr).splitlines() if "✗" in line or "FAIL" in line or "error" in line.lower()]
        return (False, errors[:20])
    except Exception as e:
        return (False, [f"CI run err: {type(e).__name__}: {e}"])


def check_narration_integrity() -> tuple[bool, list[str]]:
    """Run scan_narration_integrity.py; block on hard failures only."""
    try:
        r = subprocess.run(
            [str(SITE.parent / "S.L.A.T.E" / ".venv" / "Scripts" / "python.exe"),
             str(SITE / "scripts" / "scan_narration_integrity.py")],
            cwd=str(SITE), capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        )
        # Tool exits 2 on hard fail, 0 otherwise
        if r.returncode == 0:
            return (True, [])
        errors = [line for line in r.stdout.splitlines() if "HARD FAIL" in line or "✗" in line]
        return (False, errors[:20])
    except Exception as e:
        return (False, [f"narration integrity err: {e}"])


def check_banned_paths(config: dict) -> tuple[bool, list[str]]:
    """Check git-staged changes against never_touch_paths."""
    never = config.get("never_touch_paths", [])
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(SITE), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        staged = [p.strip() for p in r.stdout.splitlines() if p.strip()]
    except Exception as e:
        return (False, [f"git diff err: {e}"])

    violations = []
    for path in staged:
        for pattern in never:
            if path == pattern or path.startswith(pattern.rstrip("/")) or fnmatch.fnmatch(path, pattern):
                violations.append(f"BANNED PATH touched: {path} matches {pattern}")
                break
    return (len(violations) == 0, violations)


def check_size_limits(config: dict) -> tuple[bool, list[str]]:
    """Check --cached diff doesn't exceed size limits."""
    limits = config.get("size_limits", {})
    max_chars = limits.get("max_new_chars_per_commit", 8000)
    max_files = limits.get("max_files_touched_per_commit", 3)

    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            cwd=str(SITE), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        files = r.stdout.count("|")
        # Count total chars added via --numstat
        r2 = subprocess.run(
            ["git", "diff", "--cached", "--numstat"],
            cwd=str(SITE), capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        added = 0
        for line in r2.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit():
                added += int(parts[0])
        issues = []
        # Approximate: each "added line" ≈ 40 chars average
        est_chars = added * 40
        if est_chars > max_chars:
            issues.append(f"SIZE: ~{est_chars} added chars > {max_chars} max")
        if files > max_files:
            issues.append(f"SIZE: {files} files > {max_files} max")
        return (len(issues) == 0, issues)
    except Exception as e:
        return (False, [f"size check err: {e}"])


def check_hallucination_probe(draft: dict, config: dict) -> tuple[bool, list[str]]:
    """If the draft includes fact_probes, verify all pass."""
    gate = config.get("hallucination_gate", {})
    min_probes = gate.get("min_fact_probes_per_draft", 3)
    min_pass = gate.get("min_pass_rate", 1.0)

    probes = draft.get("fact_probes", []) if draft else []
    if not probes:
        return (True, ["no fact_probes on draft (uncheckable)"])
    if len(probes) < min_probes:
        return (False, [f"only {len(probes)} fact probes < {min_probes} required"])
    correct = sum(1 for p in probes if p.get("correct"))
    rate = correct / len(probes)
    if rate < min_pass:
        return (False, [f"pass rate {rate:.3f} < required {min_pass}"])
    return (True, [f"{correct}/{len(probes)} probes passed"])


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL autonomous commit gate")
    ap.add_argument("--staged", action="store_true", help="check git-staged changes")
    ap.add_argument("--draft-json", default=None, help="path to LIRIL draft JSON with fact_probes")
    ap.add_argument("--json", action="store_true", help="emit JSON result only")
    args = ap.parse_args()

    config = load_config()
    result = {"ok": True, "failures": [], "warnings": [], "checks": {}}

    ok, err = check_ci()
    result["checks"]["ci"] = {"ok": ok, "errors": err}
    if not ok:
        result["ok"] = False
        result["failures"].extend([f"CI: {e}" for e in err])

    ok, err = check_narration_integrity()
    result["checks"]["narration_integrity"] = {"ok": ok, "errors": err}
    if not ok:
        result["ok"] = False
        result["failures"].extend([f"NARRATION: {e}" for e in err])

    if args.staged:
        ok, err = check_banned_paths(config)
        result["checks"]["banned_paths"] = {"ok": ok, "errors": err}
        if not ok:
            result["ok"] = False
            result["failures"].extend(err)

        ok, err = check_size_limits(config)
        result["checks"]["size_limits"] = {"ok": ok, "errors": err}
        if not ok:
            result["ok"] = False
            result["failures"].extend(err)

    if args.draft_json:
        try:
            draft = json.loads(Path(args.draft_json).read_text(encoding="utf-8"))
        except Exception as e:
            draft = {}
            result["warnings"].append(f"draft load err: {e}")
        ok, err = check_hallucination_probe(draft, config)
        result["checks"]["hallucination_probe"] = {"ok": ok, "info": err}
        if not ok:
            result["ok"] = False
            result["failures"].extend([f"HALLUCINATION: {e}" for e in err])
        else:
            result["warnings"].extend(err)

    out = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json:
        print(out)
    else:
        print("═" * 70)
        print("LIRIL Commit Gate · SEED 118400")
        print("═" * 70)
        print(f"  verdict: {'✓ PASS' if result['ok'] else '✗ BLOCK'}")
        for check_name, check_data in result["checks"].items():
            sign = "✓" if check_data["ok"] else "✗"
            print(f"  {sign} {check_name}")
        if result["failures"]:
            print("\n  FAILURES:")
            for f in result["failures"]:
                print(f"    · {f}")
        if result["warnings"]:
            print("\n  WARNINGS:")
            for w in result["warnings"]:
                print(f"    · {w}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
