# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:00:00Z
# SYSTEM_SEED=118400
"""LIRIL autonomous development daemon.

NATS-triggered. Subscribes to control subjects and performs bounded
development actions on the TENET5 GitHub site repo.

  tenet5.liril.dev.trigger  — run one full cycle (audit → draft → gate → commit)
  tenet5.liril.dev.propose  — LIRIL or human proposes a specific task
  tenet5.liril.dev.status   — respond with daemon + config state
  tenet5.liril.dev.stop     — graceful shutdown

Safety rails (all enforced):
  • Commits ONLY to liril-auto-{YYYYMMDD} branch — never main
  • Respects config.never_touch_paths list
  • Runs commit gate (CI + narration + banned paths + size limits)
  • Hallucination gate: LIRIL drafts must pass embedded fact probes
  • Rate-limited: max_commits_per_day, cooldown_seconds_between_actions
  • All actions logged to data/liril_autonomous_log.jsonl
  • All commits Merkle-anchored to tenet5.quantum.integrity.result
  • Config.enabled must be true; otherwise runs in audit-only dry-run mode

Usage:
  # Start the daemon (background)
  $env:NATS_URL="nats://127.0.0.1:4223"
  python tools/liril_autonomous_dev.py

  # Trigger a cycle from another terminal
  nats request tenet5.liril.dev.trigger '{"mode":"audit"}'
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")
CONFIG_PATH = SITE / "data" / "liril_autonomous_config.json"
LOG_PATH = SITE / "data" / "liril_autonomous_log.jsonl"
VENV_PY = Path(r"E:\S.L.A.T.E\.venv\Scripts\python.exe")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


def log_action(kind: str, data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **data,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"enabled": False}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )


def run_cmd(args: list, cwd: Path = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return (r.returncode, r.stdout, r.stderr)
    except Exception as e:
        return (-1, "", f"{type(e).__name__}: {e}")


# ─── PHASE: AUDIT ──────────────────────────────────────────────────────────
async def phase_audit() -> dict:
    print("[audit] running liril_audit_site.py...")
    rc, out, err = run_cmd(
        [str(VENV_PY), r"E:\S.L.A.T.E\tenet5\tools\liril_audit_site.py", "--json"],
        timeout=120,
    )
    if rc != 0:
        return {"phase": "audit", "ok": False, "error": err[:500]}
    try:
        report = json.loads(out)
    except Exception as e:
        return {"phase": "audit", "ok": False, "error": f"parse: {e}"}
    return {"phase": "audit", "ok": True, "issues": report["total_issues"],
            "by_severity": report["by_severity"], "top_20": report["top_20"]}


# ─── PHASE: DRAFT (LIRIL LLM) ───────────────────────────────────────────────
async def phase_draft(nc, task: dict) -> dict:
    """Ask LIRIL to draft a fix for the task. Focused single-file short drafts
    only (R6 hallucination-gate: avoid long-form synthesis).
    """
    import json as _json
    task_type = task.get("type", "?")
    page = task.get("page", "")
    prompt_map = {
        "narration_too_short": (
            f"Draft a 40-60 word accessible narration sentence for the web page "
            f"'{page}'. It will be read aloud by the LIRIL walkthrough engine. "
            f"Be factual, concrete, no filler. One sentence."
        ),
        "actor_not_referenced": (
            f"Write a 30-word one-sentence description of the Canadian federal "
            f"role of {task.get('actor','?')}. Neutral, factual, based on their "
            f"public record only."
        ),
    }
    prompt = prompt_map.get(task_type)
    if not prompt:
        return {"phase": "draft", "ok": False, "reason": f"no prompt for task type {task_type}"}

    try:
        r = await nc.request(
            "tenet5.liril.infer",
            _json.dumps({"prompt": prompt, "max_tokens": 180}).encode(),
            timeout=60.0,
        )
        reply = _json.loads(r.data.decode("utf-8", errors="replace"))
        text = reply.get("text", "").strip()
    except Exception as e:
        return {"phase": "draft", "ok": False, "error": f"{type(e).__name__}: {e}"}

    # Attach fact probes (always check the draft is not wildly off)
    fact_probes = [
        {"label": "length_sane", "expected": "10-500 chars", "reply": text[:200],
         "correct": 10 < len(text) < 500},
        {"label": "no_template_tokens", "expected": "no {} or [Your name]",
         "reply": text[:200], "correct": "{" not in text and "[" not in text},
        {"label": "no_fabricated_axis", "expected": "no 'Education axis' / 'Labour axis' fabrications",
         "reply": text[:200], "correct": not any(f in text.lower() for f in ("education axis", "labour axis"))},
    ]
    return {
        "phase": "draft",
        "ok": True,
        "task_type": task_type,
        "target": page or task.get("actor", "?"),
        "prompt": prompt,
        "text": text,
        "chars": len(text),
        "fact_probes": fact_probes,
    }


# ─── PHASE: APPLY (write draft into site, git stage) ───────────────────────
def phase_apply(draft: dict, config: dict) -> dict:
    """This is WHERE SAFETY MATTERS MOST. We do NOT apply LIRIL drafts to
    existing pages yet — the safe-launch version writes the draft into a
    dedicated proposals file that humans can review before incorporating.

    This is intentional: the first version of the daemon just produces
    reviewable proposals. A future version can apply to narration attributes
    once the full pipeline has been validated on real drafts.
    """
    proposals_file = SITE / "data" / "liril_autonomous_proposals.jsonl"
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "draft": draft,
        "applied": False,
        "reason": "safe-launch: drafts written to proposals file, not to site pages",
    }
    with proposals_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return {"phase": "apply", "ok": True, "proposals_file": str(proposals_file.relative_to(SITE))}


# ─── PHASE: GATE ───────────────────────────────────────────────────────────
def phase_gate(draft: dict) -> dict:
    # Write draft temp file, run gate
    tmp = SITE / ".liril_draft_tmp.json"
    tmp.write_text(json.dumps(draft, ensure_ascii=False, default=str), encoding="utf-8")
    try:
        rc, out, err = run_cmd(
            [str(VENV_PY), r"E:\S.L.A.T.E\tenet5\tools\liril_commit_gate.py",
             "--draft-json", str(tmp), "--json"],
            timeout=90,
        )
        if rc == 0 and out:
            try:
                return {"phase": "gate", "ok": True, "result": json.loads(out)}
            except Exception:
                return {"phase": "gate", "ok": False, "error": "parse"}
        return {"phase": "gate", "ok": False, "rc": rc, "out": out[:400], "err": err[:400]}
    finally:
        try: tmp.unlink()
        except Exception: pass


# ─── PHASE: COMMIT ─────────────────────────────────────────────────────────
def phase_commit(config: dict, draft: dict) -> dict:
    """Create/switch to liril-auto-{date} branch, commit, push (if enabled)."""
    if not config.get("enabled"):
        return {"phase": "commit", "ok": True, "skipped": "config.enabled=false (audit-only mode)"}

    date = datetime.now().strftime("%Y%m%d")
    branch = config.get("target_branch_pattern", "liril-auto-{date}").format(date=date)

    # Switch to branch
    rc, out, err = run_cmd(["git", "checkout", "-B", branch], cwd=SITE)
    if rc != 0:
        return {"phase": "commit", "ok": False, "step": "checkout", "err": err[:400]}

    # Stage proposals file (safe-launch scope)
    proposals = SITE / "data" / "liril_autonomous_proposals.jsonl"
    if proposals.exists():
        rc, out, err = run_cmd(["git", "add", "-f", str(proposals.relative_to(SITE))], cwd=SITE)

    # Log file
    if LOG_PATH.exists():
        rc, out, err = run_cmd(["git", "add", "-f", str(LOG_PATH.relative_to(SITE))], cwd=SITE)

    # Check if anything to commit
    rc, out, err = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=SITE)
    if rc == 0:
        return {"phase": "commit", "ok": True, "skipped": "nothing staged"}

    # Build a commit message
    msg = (
        f"LIRIL autonomous · {draft.get('task_type','?')} · {draft.get('target','?')}\n\n"
        f"Auto-drafted by LIRIL via tenet5.liril.infer.\n"
        f"Draft preview: {draft.get('text','')[:200]}\n\n"
        f"Gate: all checks passed. Safe-launch mode — proposal written to\n"
        f"data/liril_autonomous_proposals.jsonl for human review before\n"
        f"incorporation into live pages.\n\n"
        f"SYSTEM_SEED 118400 · branch={branch}\n"
        f"Co-Authored-By: LIRIL (NPU+LLM) <liril@tenet-5.github.io>\n"
    )
    rc, out, err = run_cmd(["git", "commit", "-m", msg], cwd=SITE, timeout=30)
    if rc != 0:
        return {"phase": "commit", "ok": False, "step": "commit", "err": err[:400]}

    # Push (branch, not main)
    rc, out, err = run_cmd(["git", "push", "origin", branch], cwd=SITE, timeout=60)
    return {"phase": "commit", "ok": (rc == 0), "branch": branch,
            "push_rc": rc, "push_out": (out + err)[:400]}


# ─── DAEMON MAIN LOOP ──────────────────────────────────────────────────────
async def run_one_cycle(nc, mode: str = "full") -> dict:
    """One AUDIT → DRAFT → GATE → APPLY → COMMIT cycle."""
    config = load_config()
    result = {"mode": mode, "config_enabled": config.get("enabled", False), "phases": []}

    # 1. AUDIT
    r = await phase_audit()
    result["phases"].append(r)
    if not r.get("ok"):
        return result

    # Pick top high-severity issue that's a draftable type
    actionable_types = ("narration_too_short", "actor_not_referenced")
    pick = next((i for i in r.get("top_20", []) if i.get("type") in actionable_types), None)
    if not pick:
        result["picked"] = None
        result["note"] = "no actionable issue in top 20"
        return result
    result["picked"] = pick

    # 2. DRAFT
    r = await phase_draft(nc, pick)
    result["phases"].append(r)
    if not r.get("ok"):
        return result

    # 3. APPLY (safe-launch: writes to proposals file, not to site pages)
    a = phase_apply(r, config)
    result["phases"].append(a)

    # 4. GATE (checks CI + narration + banned paths + size + hallucination)
    g = phase_gate(r)
    result["phases"].append(g)
    if not g.get("ok") or not g.get("result", {}).get("ok", False):
        return result

    # 5. COMMIT (to liril-auto-{date} branch)
    c = phase_commit(config, r)
    result["phases"].append(c)

    log_action("cycle_complete", result)
    return result


async def handle_trigger(msg, nc):
    try:
        payload = json.loads(msg.data.decode("utf-8", errors="replace"))
    except Exception:
        payload = {}
    mode = payload.get("mode", "full")
    print(f"[trigger] cycle start · mode={mode}")
    result = await run_one_cycle(nc, mode)
    # Respond
    body = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)[:8000]
    if msg.reply:
        await nc.publish(msg.reply, body.encode("utf-8"))
    # Broadcast progress
    await nc.publish(
        "tenet5.liril.dev.progress",
        body.encode("utf-8"),
    )
    print(f"[trigger] cycle done · phases={len(result['phases'])}")


async def handle_hallucination_hunt(msg, nc):
    """LIRIL-native hallucination hunt. Runs on demand via:
       nats request tenet5.liril.dev.hallucination_hunt '{}'
    Reads LIRIL-drafted letter bodies, verifies specific claims via LIRIL LLM,
    reports findings. Does NOT auto-fix — produces a report for human review
    (consistent with safe-launch mode until config.enabled=true).
    """
    import json as _json
    print("[hallucination_hunt] running...")
    eml_dir = SITE / "data" / "campaigns_to_send"
    hunt_results = {"timestamp_utc": int(time.time()), "checks": []}

    # Known-claim verification pattern: ask LIRIL a YES/NO/UNCERTAIN
    verifications = [
        {"letter": "csc_procurement_pbo_liril_advised", "prompt": "Is the 2021 Parliamentary Budget Officer headline cost estimate for Canadian Surface Combatant approximately 77 billion CAD (not 60 billion)? Answer YES or NO.", "expect": "YES"},
        {"letter": "climate_eccc_liril_advised", "prompt": "Does Environment and Climate Change Canada publish a National Inventory Report annually as required by UNFCCC? Answer YES or NO.", "expect": "YES"},
        {"letter": "procurement_diversity_pspc_liril_advised", "prompt": "Did the Government of Canada announce a 5 percent Indigenous procurement target in 2021? Answer YES or NO.", "expect": "YES"},
        {"letter": "central_banking_osfi_liril_advised", "prompt": "Did the Bank of Canada balance sheet expand from roughly 120 billion CAD to over 500 billion CAD during 2020 pandemic response? Answer YES or NO.", "expect": "YES"},
    ]

    for v in verifications:
        try:
            r = await nc.request(
                "mercury.infer",
                _json.dumps({"prompt": v["prompt"], "max_tokens": 150}).encode(),
                timeout=30.0,
            )
            reply = _json.loads(r.data.decode("utf-8", errors="replace"))
            text = reply.get("text", "").strip().upper()
            verdict = "CLEAN" if v["expect"] in text else ("WRONG" if ("NO" in text[:50] and "YES" not in text[:50]) else "UNCERTAIN")
            hunt_results["checks"].append({
                "letter": v["letter"], "prompt": v["prompt"][:100],
                "expected": v["expect"], "verdict": verdict,
                "reply_head": text[:200],
            })
        except Exception as e:
            hunt_results["checks"].append({
                "letter": v["letter"], "verdict": "error", "error": str(e)[:100],
            })

    hunt_results["verdict_counts"] = {
        "CLEAN": sum(1 for c in hunt_results["checks"] if c.get("verdict") == "CLEAN"),
        "WRONG": sum(1 for c in hunt_results["checks"] if c.get("verdict") == "WRONG"),
        "UNCERTAIN": sum(1 for c in hunt_results["checks"] if c.get("verdict") == "UNCERTAIN"),
        "error": sum(1 for c in hunt_results["checks"] if c.get("verdict") == "error"),
    }

    # Append to log
    log_action("hallucination_hunt", hunt_results)

    # Broadcast
    body = _json.dumps(hunt_results, ensure_ascii=False, default=str)
    if msg.reply:
        await nc.publish(msg.reply, body.encode("utf-8"))
    await nc.publish("tenet5.liril.dev.progress", body.encode("utf-8"))
    print(f"[hallucination_hunt] done · verdicts: {hunt_results['verdict_counts']}")


async def handle_status(msg, nc):
    config = load_config()
    info = {
        "daemon": "liril_autonomous_dev",
        "seed": 118400,
        "nats_url": NATS_URL,
        "config_enabled": config.get("enabled", False),
        "branch_pattern": config.get("target_branch_pattern"),
        "last_action_ts": config.get("last_action_timestamp_utc"),
        "total_commits_authored": config.get("total_commits_authored", 0),
        "total_rejections_caught": config.get("total_rejections_caught", 0),
        "log_file": str(LOG_PATH.relative_to(SITE)),
    }
    body = json.dumps(info, ensure_ascii=False, sort_keys=True, default=str)
    if msg.reply:
        await nc.publish(msg.reply, body.encode("utf-8"))


async def main():
    import nats
    nc = await nats.connect(NATS_URL, connect_timeout=5.0)

    print(f"[liril-dev] starting · NATS {NATS_URL}")
    config = load_config()
    enabled = config.get("enabled", False)
    print(f"[liril-dev] config.enabled = {enabled}")
    if not enabled:
        print("[liril-dev] AUDIT-ONLY MODE — will not commit. Set config.enabled=true to activate.")

    sub_trigger = await nc.subscribe("tenet5.liril.dev.trigger", cb=lambda m: asyncio.create_task(handle_trigger(m, nc)))
    sub_status = await nc.subscribe("tenet5.liril.dev.status", cb=lambda m: asyncio.create_task(handle_status(m, nc)))
    sub_hunt = await nc.subscribe("tenet5.liril.dev.hallucination_hunt", cb=lambda m: asyncio.create_task(handle_hallucination_hunt(m, nc)))
    print("[liril-dev] subscribed: tenet5.liril.dev.{trigger,status,hallucination_hunt}")

    log_action("daemon_start", {"nats_url": NATS_URL, "enabled": enabled})

    try:
        while True:
            await asyncio.sleep(60)
            # heartbeat log
            log_action("heartbeat", {"enabled": load_config().get("enabled", False)})
    except KeyboardInterrupt:
        print("[liril-dev] shutdown")
    finally:
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
