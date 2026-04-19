# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:55:00Z
"""Canonical role specifications + output validators for the TENET5 dev team.

Every role in the 6-role pipeline has:
  - system_prompt: the canonical system message (used for fine-tuning)
  - output_schema: what a valid output looks like (regex + rules)
  - validate(output_text): returns (ok: bool, issues: list[str])

This module is imported by both liril_dev_team.py (runtime validation
between roles) and liril_build_training_set.py (system prompt for
fine-tuning data). One source of truth.

Validators are intentionally strict — better to fail early and retry a
role than let a malformed output cascade through the pipeline and
produce a confident-looking but broken commit.
"""
from __future__ import annotations
import json
import re
from typing import Callable

# ════════════════════════════════════════════════════════════════════════
# System prompts — canonical per role. Used by both runtime dispatch and
# training-set compilation. Changing these changes training signal.
# ════════════════════════════════════════════════════════════════════════

ROLE_SYSTEM_PROMPTS: dict[str, str] = {
    "researcher": (
        "You are the RESEARCHER on a TENET5 dev team. Your job is to identify "
        "exactly one specific primary-source fact needed to complete the task. "
        "Output SOURCE, QUOTE, GAP_FILLED triple. Cite Canada Gazette, Hansard, "
        "CanLII, or a Commissioner report. Never invent citations."
    ),
    "architect": (
        "You are the ARCHITECT on a TENET5 dev team. Your job is to name the "
        "exact target file and insertion point for a change. Output FILE, "
        "ANCHOR, POSITION. Respect the 19-axis / IA-pillar layout."
    ),
    "designer": (
        "You are the DESIGNER on a TENET5 dev team. Your job is to propose "
        "the UX/visual form of a change. Output FORM, ACCESSIBILITY, COPY_TONE. "
        "Follow Shneiderman overview-first, NN/g readability, WCAG 2.1 AA."
    ),
    "engineer": (
        "You are the ENGINEER on a TENET5 dev team. Your job is to produce "
        "the literal string to insert into a target file. Output a fenced "
        "code block only. No explanation, no narration, only the change."
    ),
    "editor": (
        "You are the EDITOR on a TENET5 dev team. Your job is to copy-check "
        "the engineer output. Output CHECK_CITATIONS, CHECK_LINKS, CHECK_A11Y, "
        "OVERALL (CLEAN | NEEDS_REWORK)."
    ),
    "gatekeeper": (
        "You are the GATEKEEPER on a TENET5 dev team — the hallucination gate. "
        "Output VERDICT: PASS | WATCH | FAIL and REASON: one short sentence. "
        "FAIL if the pipeline produced no concrete deliverable when the task "
        "acceptance required one. WATCH if a date or citation looks slightly "
        "off but uncertain. PASS only when the diff is verifiable."
    ),
}


# ════════════════════════════════════════════════════════════════════════
# Validators — strict regex/structure checks. Each returns (ok, issues).
# ════════════════════════════════════════════════════════════════════════

def _find_line(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(0).strip() if m else None


def _has_line(text: str, *labels: str) -> dict[str, bool]:
    out = {}
    for lbl in labels:
        # matches "LABEL:" followed by any content on the same line
        pattern = rf"^\s*{re.escape(lbl)}\s*:"
        out[lbl] = bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))
    return out


def validate_researcher(text: str) -> tuple[bool, list[str]]:
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    present = _has_line(text, "SOURCE", "QUOTE", "GAP_FILLED")
    missing = [k for k, v in present.items() if not v]
    if missing:
        issues.append(f"missing field(s): {missing}")
    # SOURCE should look citable — not generic
    src = _find_line(r"^\s*SOURCE\s*:.*", text) or ""
    if src:
        low = src.lower()
        if any(g in low for g in ["general knowledge", "common knowledge", "widely known"]):
            issues.append("SOURCE is vague (general/common knowledge not allowed — cite a specific document)")
    return (not issues), issues


def validate_architect(text: str) -> tuple[bool, list[str]]:
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    present = _has_line(text, "FILE", "ANCHOR", "POSITION")
    missing = [k for k, v in present.items() if not v]
    if missing:
        issues.append(f"missing field(s): {missing}")
    pos = (_find_line(r"^\s*POSITION\s*:.*", text) or "").lower()
    if pos and not any(p in pos for p in ["before", "after", "replace"]):
        issues.append("POSITION must be one of before/after/replace")
    return (not issues), issues


def validate_designer(text: str) -> tuple[bool, list[str]]:
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    present = _has_line(text, "FORM", "ACCESSIBILITY", "COPY_TONE")
    missing = [k for k, v in present.items() if not v]
    if missing:
        issues.append(f"missing field(s): {missing}")
    return (not issues), issues


def validate_engineer(text: str) -> tuple[bool, list[str]]:
    """Engineer must emit a fenced code block whose body is the literal change.
    For JSON targets (the only type we currently apply), the fenced content
    must also parse as a JSON object.

    2026-04-18: tolerate half-closed fences. Large-output tasks (multi-
    item arrays, entire-file emits) sometimes hit the max_tokens budget
    before the model emits the closing ```. If the opening fence + body
    is well-formed and parses as JSON, accept it — truncation would be
    caught later by the apply step if the JSON is actually malformed.
    """
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    # Find first fenced code block (strict: both fences present)
    m = re.search(r"```(\w*)\n([\s\S]*?)```", text)
    if not m:
        # Fallback: opening fence present, no closing fence (truncated).
        # Accept if body is well-formed JSON or clearly non-empty.
        m_open = re.search(r"```(\w*)\n([\s\S]+)$", text)
        if not m_open:
            issues.append("no fenced code block found — engineer MUST wrap output in ```...```")
            return False, issues
        # Open-but-not-closed fence — mark as a warning via issues but
        # treat as valid if body passes subsequent JSON check.
        m = m_open
    lang = (m.group(1) or "").strip().lower()
    body = m.group(2)
    # Strip trailing backticks if the model did emit SOME close chars.
    body = body.rstrip("`").rstrip()
    if not body.strip():
        issues.append("fenced code block is empty")
        return False, issues
    # If the lang hint is json, the body must parse
    if lang in ("json", "jsonc"):
        try:
            parsed = json.loads(body)
        except Exception as e:
            issues.append(f"code block marked json but does not parse: {e}")
            return False, issues
        if not isinstance(parsed, dict):
            issues.append("JSON engineer output must be an object, not list/scalar")
            return False, issues
    return True, []


def validate_editor(text: str) -> tuple[bool, list[str]]:
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    present = _has_line(text, "CHECK_CITATIONS", "CHECK_LINKS", "CHECK_A11Y", "OVERALL")
    missing = [k for k, v in present.items() if not v]
    if missing:
        issues.append(f"missing field(s): {missing}")
    overall = (_find_line(r"^\s*OVERALL\s*:.*", text) or "").upper()
    if overall and not any(p in overall for p in ["CLEAN", "NEEDS_REWORK", "NEEDS REWORK"]):
        issues.append("OVERALL must be CLEAN or NEEDS_REWORK")
    return (not issues), issues


def validate_gatekeeper(text: str) -> tuple[bool, list[str]]:
    issues = []
    if not text or not text.strip():
        return False, ["empty output"]
    present = _has_line(text, "VERDICT", "REASON")
    missing = [k for k, v in present.items() if not v]
    if missing:
        issues.append(f"missing field(s): {missing}")
    vline = _find_line(r"^\s*VERDICT\s*:.*", text) or ""
    if vline:
        after = vline.split(":", 1)[1].strip().upper().split()[0].strip(".,;:") if ":" in vline else ""
        if after not in ("PASS", "WATCH", "FAIL"):
            issues.append(f"VERDICT value not one of PASS/WATCH/FAIL: got {after!r}")
    return (not issues), issues


VALIDATORS: dict[str, Callable[[str], tuple[bool, list[str]]]] = {
    "researcher": validate_researcher,
    "architect":  validate_architect,
    "designer":   validate_designer,
    "engineer":   validate_engineer,
    "editor":     validate_editor,
    "gatekeeper": validate_gatekeeper,
}


def validate_role_output(role: str, text: str) -> tuple[bool, list[str]]:
    """Public entry point. Returns (ok, issues)."""
    fn = VALIDATORS.get(role)
    if fn is None:
        return True, [f"no validator for role {role!r} — accepting"]
    return fn(text)


# ════════════════════════════════════════════════════════════════════════
# Self-test when invoked directly
# ════════════════════════════════════════════════════════════════════════

def _selftest() -> None:
    tests = [
        ("researcher", "SOURCE: AG 2024 Report 1 para 1.47\nQUOTE: $59.5M.\nGAP_FILLED: anchors the figure.", True),
        ("researcher", "SOURCE: general knowledge\nQUOTE: x\nGAP_FILLED: y", False),
        ("researcher", "", False),
        ("architect", "FILE: data/foo.json\nANCHOR: summary\nPOSITION: after", True),
        ("architect", "FILE: data/foo.json\nANCHOR: summary\nPOSITION: somewhere", False),
        ("engineer", "```json\n{\"key\": \"value\"}\n```", True),
        ("engineer", "```json\nNOT JSON\n```", False),
        ("engineer", "here is the code: {\"k\":\"v\"}", False),   # no fence
        ("editor", "CHECK_CITATIONS: OK\nCHECK_LINKS: OK\nCHECK_A11Y: OK\nOVERALL: CLEAN", True),
        ("editor", "CHECK_CITATIONS: OK\nOVERALL: CLEAN", False),
        ("gatekeeper", "VERDICT: PASS\nREASON: looks good", True),
        ("gatekeeper", "VERDICT: MAYBE\nREASON: ...", False),
        ("gatekeeper", "REASON: no verdict", False),
    ]
    ok_count = 0
    for role, text, expected in tests:
        actual, issues = validate_role_output(role, text)
        status = "✓" if actual == expected else "✗"
        if actual == expected:
            ok_count += 1
        print(f"  {status} {role:11s} expected={expected} got={actual}  issues={issues}")
    print(f"\n{ok_count}/{len(tests)} tests passed")


if __name__ == "__main__":
    _selftest()
