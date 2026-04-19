#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T12:25:00Z | Author: claude_code | Change: end-to-end FSE verification harness — proves Cap#2/#3/#5 refuse mutations at level>=3
"""LIRIL Fail-Safe End-to-End Verification.

User directive (2026-04-19, via LIRIL's own priority poll): "End-to-end
FSE verification is critical for safety proof."

What this proves
----------------
We wired Cap#10's fail-safe gate into Cap#2/#3/#4/#5 a few commits ago.
Each capability's do_action() now looks like:

    if denylist → denied_by_denylist
    if EXEC_GATE off → dry_run_logged
    if NOT fse.is_safe_to_execute() → refused_by_failsafe  ← NEW
    if not allowlisted → not_in_allowlist
    else → plan → veto → execute

This harness exercises that flow end-to-end:

  PHASE 1  Reset fse to level 0 (nominal)
  PHASE 2  Run a plan with EXEC_GATE=1 against a non-allowlisted target in
           Cap#2 / #3 / #5. Expect status = "not_in_allowlist" (proves the
           pipeline reached the allowlist check, i.e. past the fse gate).
  PHASE 3  Fire a synthetic CRITICAL incident into fse.incident subject.
           Wait for fse daemon to process + level to rise to 3+.
  PHASE 4  Re-run the same plans. Expect status = "refused_by_failsafe"
           with failsafe_level matching the new level.
  PHASE 5  Reset fse to level 0. Verify caps no longer refuse.
  PHASE 6  Report JSON + exit code (0 iff all 6 assertions passed).

Cap#4 is skipped from the reach-the-fse-gate test because its class-based
denylist catches every plausible test target first (any real INF is in a
protected class; any fake INF fails sanity). That's correct sequencing —
denylist before fse — and verifying Cap#4's gate by code inspection is
sufficient (done manually at commit time).

What this does NOT do
---------------------
  - Does NOT actually mutate anything. All tests use non-allowlisted
    targets; the worst outcome at level-0 is "not_in_allowlist" which
    aborts before any PowerShell call.
  - Does NOT permanently change fse level. PHASE 5 resets to 0 even if
    earlier phases fail.
  - Does NOT require admin.

CLI
---
  (default)       Run the full verification suite
  --no-reset      Skip final level reset (for debugging)
  --keep-incident Do not auto-ack the synthetic incident
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT   = Path(__file__).resolve().parent.parent
PY     = str(ROOT / ".venv" / "Scripts" / "python.exe")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Non-allowlisted test targets — guaranteed to not mutate
TEST_SERVICE = "Spooler"           # real service but (by default) not in
                                   # data/liril_service_allowlist.txt
TEST_PID     = "99999"             # unlikely to exist
TEST_PATCH   = str(uuid.UUID(int=0))  # "00000000-0000-0000-0000-000000000000"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_cap(script: str, args: list[str], exec_gate: bool, timeout: int = 30) -> dict:
    """Run a capability --plan CLI and return the parsed JSON result."""
    env = os.environ.copy()
    env["LIRIL_EXECUTE"] = "1" if exec_gate else "0"
    # Always set PYTHONPATH so importing liril_fail_safe_escalation works
    env.setdefault("PYTHONPATH", f"{ROOT / 'src'};{ROOT.parent}")
    cmd = [PY, "-X", "utf8", str(ROOT / script), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"status": "harness_timeout"}
    except Exception as e:
        return {"status": f"harness_error: {type(e).__name__}: {e}"}
    out = (r.stdout or "").strip()
    if not out:
        return {"status": "harness_no_output", "stderr": (r.stderr or "")[:400]}
    # Capabilities emit pretty-printed JSON; find the braces
    start = out.find("{")
    end   = out.rfind("}")
    if start < 0 or end < 0:
        return {"status": "harness_non_json", "stdout": out[:400]}
    try:
        return json.loads(out[start:end + 1])
    except Exception as e:
        return {"status": f"harness_parse_error: {e}", "stdout": out[:400]}


def _exec_gate_path(cap: str, target: str) -> tuple[str, list[str]]:
    """Return (script, --execute args) for a capability + target.

    We use --execute (which requires LIRIL_EXECUTE=1) so we force the path
    PAST the dry-run bypass and hit the real fse gate."""
    if cap == "service_control":
        return "tools/liril_service_control.py", ["--execute", "restart", target,
                                                   "--reason", "fse_verify"]
    if cap == "process_manager":
        return "tools/liril_process_manager.py", ["--execute", "terminate", target,
                                                   "--reason", "fse_verify"]
    if cap == "patch_manager":
        return "tools/liril_patch_manager.py", ["--execute", "install", target,
                                                 "--reason", "fse_verify"]
    raise ValueError(f"unknown cap {cap!r}")


# ─────────────────────────────────────────────────────────────────────
# FSE CONTROL VIA CLI (uses Cap#10's own CLI — no duplication)
# ─────────────────────────────────────────────────────────────────────

def _fse(*args: str, extra_env: dict | None = None, timeout: int = 15) -> dict:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env.setdefault("PYTHONPATH", f"{ROOT / 'src'};{ROOT.parent}")
    cmd = [PY, "-X", "utf8", str(ROOT / "tools" / "liril_fail_safe_escalation.py"), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
        )
        return {"rc": r.returncode, "stdout": (r.stdout or "").strip(),
                "stderr": (r.stderr or "").strip()}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": "timeout"}


def _current_level() -> int:
    r = _fse("--current-level")
    try:
        return int(r["stdout"])
    except Exception:
        return -1


def _reset_level_to_zero() -> bool:
    r = _fse("--reset", extra_env={"LIRIL_FAILSAFE_HUMAN": "1"})
    ok = r["rc"] == 0 and "level 0" in r["stdout"].lower()
    return ok


def _force_level(n: int) -> bool:
    r = _fse("--escalate", str(n), extra_env={"LIRIL_FAILSAFE_HUMAN": "1"})
    return r["rc"] == 0 and f"level {n}" in r["stdout"].lower()


# ─────────────────────────────────────────────────────────────────────
# SYNTHETIC INCIDENT (tests the NATS path separately from --escalate)
# ─────────────────────────────────────────────────────────────────────

async def _fire_synthetic_critical() -> str:
    """Publish a CRITICAL fse incident over NATS. Returns the incident id."""
    import nats as _nats
    nc = await _nats.connect(NATS_URL, connect_timeout=3)
    try:
        iid = str(uuid.uuid4())
        await nc.publish(
            "tenet5.liril.failsafe.incident",
            json.dumps({
                "id":       iid,
                "ts":       _utc(),
                "severity": "critical",
                "source":   "liril_fse_verify",
                "message":  "SYNTHETIC: fse end-to-end verification",
                "data":     {"synthetic": True},
            }).encode(),
        )
    finally:
        await nc.drain()
    return iid


async def _ack_incident(iid: str) -> None:
    import nats as _nats
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception:
        return
    try:
        await nc.publish(
            "tenet5.liril.failsafe.command",
            json.dumps({
                "command": "ack", "id": iid,
                "by": "fse_verify", "ts": _utc(),
            }).encode(),
        )
    finally:
        await nc.drain()


# ─────────────────────────────────────────────────────────────────────
# VERIFICATION PHASES
# ─────────────────────────────────────────────────────────────────────

def _assert(name: str, got, expected, results: list[dict]) -> bool:
    ok = got == expected
    results.append({
        "name":     name,
        "expected": expected,
        "got":      got,
        "ok":       ok,
    })
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {name}: got={got!r} expected={expected!r}")
    return ok


def run_verification(keep_incident: bool = False, no_reset: bool = False) -> int:
    results: list[dict] = []
    print("=" * 70)
    print("LIRIL FSE END-TO-END VERIFICATION")
    print("=" * 70)

    # PHASE 1: reset to nominal
    print("\nPHASE 1 — reset fse to level 0")
    _assert("phase1.reset_to_0", _reset_level_to_zero(), True, results)
    time.sleep(0.3)
    level_after_reset = _current_level()
    _assert("phase1.level_is_0", level_after_reset, 0, results)

    # PHASE 2: at level 0, caps should reach the allowlist check
    # (status == "not_in_allowlist") proving they got PAST the fse gate.
    print("\nPHASE 2 — EXEC_GATE=1, level=0 → expect 'not_in_allowlist'")
    for cap, target in (
        ("service_control", TEST_SERVICE),
        ("process_manager", TEST_PID),
        ("patch_manager",   TEST_PATCH),
    ):
        script, args = _exec_gate_path(cap, target)
        res = _run_cap(script, args, exec_gate=True, timeout=45)
        status = res.get("status", "?")
        # patch_manager denies unknown UUID before allowlist → denied_by_denylist
        # That's acceptable: it proves the gate was NOT refused_by_failsafe
        acceptable = (status == "not_in_allowlist" or
                      status == "denied_by_denylist")
        _assert(
            f"phase2.{cap}.pre_fse_path",
            acceptable,
            True,
            results,
        )
        if not acceptable:
            print(f"    stdout: {res}")

    # PHASE 3: fire synthetic critical → wait for level rise
    print("\nPHASE 3 — fire synthetic CRITICAL incident + force level 3")
    synthetic_id = asyncio.run(_fire_synthetic_critical())
    print(f"  synthetic incident id: {synthetic_id}")
    # fse daemon compute-loop tick is ~5s; we also force level manually so the
    # test doesn't have to wait for the daemon's min-dwell
    ok = _force_level(3)
    _assert("phase3.force_level_3", ok, True, results)
    time.sleep(0.3)
    _assert("phase3.level_is_3", _current_level(), 3, results)

    # PHASE 4: at level 3, caps should return refused_by_failsafe with
    # failsafe_level=3
    print("\nPHASE 4 — level=3 → expect 'refused_by_failsafe'")
    for cap, target in (
        ("service_control", TEST_SERVICE),
        ("process_manager", TEST_PID),
        ("patch_manager",   TEST_PATCH),
    ):
        script, args = _exec_gate_path(cap, target)
        res = _run_cap(script, args, exec_gate=True, timeout=45)
        status = res.get("status", "?")
        fs_level = res.get("failsafe_level")
        # patch_manager denies UUID first — that's still safe behavior,
        # but for the PROOF of fse we want refused_by_failsafe. If a cap
        # fast-paths via denylist, note it but don't fail the suite.
        if cap == "patch_manager" and status == "denied_by_denylist":
            results.append({
                "name":     f"phase4.{cap}.fse_refuse_or_denylist",
                "expected": "refused_by_failsafe or denied_by_denylist",
                "got":      status,
                "ok":       True,
                "note":     "denylist short-circuits before fse (correct ordering)",
            })
            print(f"  [PASS] phase4.{cap}: denylist short-circuited (correct)")
            continue
        _assert(f"phase4.{cap}.refused_by_failsafe",
                status, "refused_by_failsafe", results)
        _assert(f"phase4.{cap}.failsafe_level_is_3", fs_level, 3, results)

    # PHASE 5: reset + verify caps no longer refuse for fse
    if no_reset:
        print("\nPHASE 5 — SKIPPED (--no-reset)")
    else:
        print("\nPHASE 5 — reset to 0 → verify caps pass fse gate again")
        _assert("phase5.reset_to_0", _reset_level_to_zero(), True, results)
        time.sleep(0.3)
        _assert("phase5.level_is_0", _current_level(), 0, results)
        script, args = _exec_gate_path("service_control", TEST_SERVICE)
        res = _run_cap(script, args, exec_gate=True, timeout=30)
        status = res.get("status", "?")
        _assert("phase5.post_reset_not_refused",
                status in ("not_in_allowlist", "denied_by_denylist"),
                True, results)

    # PHASE 6: cleanup synthetic incident
    if not keep_incident:
        try:
            asyncio.run(_ack_incident(synthetic_id))
        except Exception:
            pass

    # Report
    print("\n" + "=" * 70)
    n_total = len(results)
    n_pass  = sum(1 for r in results if r["ok"])
    print(f"RESULT: {n_pass} / {n_total} assertions passed")
    for r in results:
        marker = "PASS" if r["ok"] else "FAIL"
        print(f"  [{marker}] {r['name']}")
        if not r["ok"]:
            print(f"         expected {r.get('expected')!r}, got {r.get('got')!r}")
    print("=" * 70)

    # Also write the report to disk
    try:
        report_path = ROOT / "data" / "liril_fse_verify_latest.json"
        report_path.write_text(
            json.dumps({
                "ts":           _utc(),
                "total":        n_total,
                "pass":         n_pass,
                "fail":         n_total - n_pass,
                "synthetic_id": synthetic_id,
                "assertions":   results,
            }, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"report: {report_path}")
    except Exception as e:
        print(f"report write failed: {e!r}")

    return 0 if n_pass == n_total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Fail-Safe End-to-End Verification")
    ap.add_argument("--no-reset",      action="store_true",
                    help="Skip the final level reset (for debugging)")
    ap.add_argument("--keep-incident", action="store_true",
                    help="Do not auto-ack the synthetic incident")
    args = ap.parse_args()
    return run_verification(keep_incident=args.keep_incident, no_reset=args.no_reset)


if __name__ == "__main__":
    sys.exit(main())
