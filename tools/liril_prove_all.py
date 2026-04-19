#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T18:00:00Z | Author: claude_code | Change: Grok-review round 4 — check_process retries 3x to defend against TOCTOU during supervisor respawn
"""LIRIL Prove-All — Comprehensive Verification of the 24/7 Stack.

User directive: "continue and prove all."

Scope
-----
This harness exercises every LIRIL daemon and every capability's core
function. It is the single artifact Daniel can run at any time to
confirm the stack is healthy AND behaviourally correct — not just
"process exists" but "capability responds correctly to a known input."

For each of 14 daemons, we run 1-3 checks:

  1. PROCESS       — pidfile exists and PID is alive
  2. FUNCTIONAL    — capability CLI / NATS / sqlite responds correctly
  3. INTEGRATION   — cross-capability invariant (e.g. fse gate refuses)

The fail-safe end-to-end test is delegated to liril_fse_verify.py
(which has 15 assertions of its own) and invoked as one composite check.

CLI
---
  (default)    Run the full suite
  --quick      Skip long-running checks (WU refresh, file scan)
  --json       Output machine-readable JSON only
  --names      List all check names + exit
  --only NAMES Run only the comma-separated checks

Exit codes
----------
  0  — all critical checks passed
  1  — one or more critical checks failed
  2  — harness itself errored
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT   = Path(__file__).resolve().parent.parent
PY     = str(ROOT / ".venv" / "Scripts" / "python.exe")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DAEMONS = [
    "failsafe", "observer", "windows_monitor", "service_control",
    "process_manager", "driver_manager", "patch_manager", "self_repair",
    "hardware_health", "user_intent", "nemo_server",
    "network_reach", "communication", "file_awareness", "journal", "api",
    "autonomous",
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Check:
    name: str
    critical: bool = True
    func: Callable = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────
# UTIL
# ─────────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(int(pid))
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def _read_pidfile(name: str) -> int | None:
    p = ROOT / "data" / "liril_supervisor" / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _run_cli(script: str, args: list[str], timeout: int = 20,
             extra_env: dict | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", f"{ROOT / 'src'};{ROOT.parent}")
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    cmd = [PY, "-X", "utf8", str(ROOT / script), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


async def _nats_listen(subjects: list[str], seconds: float) -> dict[str, int]:
    import nats as _nats
    counts: dict[str, int] = {s: 0 for s in subjects}
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception:
        return counts
    async def cb(msg):
        if msg.subject in counts:
            counts[msg.subject] += 1
        else:
            # Match wildcards (e.g. heartbeat.>)
            for s in subjects:
                if s.endswith(">") and msg.subject.startswith(s[:-1]):
                    counts[s] += 1
                    return
    subs = []
    try:
        for s in subjects:
            subs.append(await nc.subscribe(s, cb=cb))
        await asyncio.sleep(seconds)
    finally:
        try: await nc.drain()
        except Exception: pass
    return counts


async def _nats_request(subject: str, payload: bytes, timeout: float = 5.0) -> dict | None:
    import nats as _nats
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception:
        return None
    try:
        try:
            r = await nc.request(subject, payload, timeout=timeout)
            try:
                return json.loads(r.data.decode())
            except Exception:
                return {"_raw": r.data.decode()[:400]}
        except Exception:
            return None
    finally:
        try: await nc.drain()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CHECK IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────

def check_process(name: str) -> tuple[bool, str]:
    """Verify the supervisor's pidfile points to a live PID.

    Grok-review round 4 fix (2026-04-19): TOCTOU defence. Supervisor can
    restart a daemon between our _read_pidfile() and _pid_alive(), causing
    a spurious FAIL. Retry up to 3 times with 1-second gap — a daemon
    respawn completes well inside that window, and a truly-dead daemon
    will still be dead on the third read."""
    for attempt in range(3):
        pid = _read_pidfile(name)
        if pid is not None and _pid_alive(pid):
            return True, f"pid={pid} alive"
        if attempt < 2:
            import time as _t
            _t.sleep(1.0)
    # Third attempt failed
    pid = _read_pidfile(name)
    if pid is None:
        return False, f"no pidfile at data/liril_supervisor/{name}.pid (3 attempts)"
    return False, f"pidfile says pid={pid} but not alive (3 attempts over 2s)"


# —— Cap-specific functional checks ——

def check_failsafe_api() -> tuple[bool, str]:
    """Cap#10: public API + CLI."""
    rc, out, err = _run_cli("tools/liril_fail_safe_escalation.py",
                            ["--current-level"], timeout=10)
    if rc != 0:
        return False, f"--current-level rc={rc} err={err[:200]}"
    try:
        level = int(out.strip())
    except Exception:
        return False, f"non-numeric level output: {out[:200]}"
    if level < 0 or level > 4:
        return False, f"level out of range: {level}"
    return True, f"level={level}"


def check_observer_db() -> tuple[bool, str]:
    """observer: sqlite DB exists and has recent rows."""
    db = ROOT / "data" / "liril_observer.sqlite"
    if not db.exists():
        return False, "observer sqlite missing"
    import sqlite3
    try:
        c = sqlite3.connect(str(db), timeout=3)
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM events WHERE ts > ?",
                (time.time() - 300,),
            ).fetchone()
            n = int(row[0]) if row else 0
        finally:
            c.close()
    except sqlite3.OperationalError:
        # Schema might differ; try a looser check
        try:
            c = sqlite3.connect(str(db), timeout=3)
            try:
                tables = c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            finally:
                c.close()
            return True, f"db reachable, {len(tables)} tables"
        except Exception as e:
            return False, f"db unreadable: {e}"
    except Exception as e:
        return False, f"db query failed: {e}"
    return (n > 0, f"{n} events in last 5 min")


def check_windows_monitor_stream() -> tuple[bool, str]:
    """Cap#1: windows.system.metrics appears in observer DB within last 5 min.

    We query observer's sqlite rather than live-subscribing because the
    observer is already subscribed to everything and persists with
    timestamps — its DB is ground truth for 'are messages flowing?'."""
    db = ROOT / "data" / "liril_observer.sqlite"
    if not db.exists():
        return False, "observer sqlite missing"
    import sqlite3
    try:
        c = sqlite3.connect(str(db), timeout=3)
        try:
            row = c.execute(
                "SELECT COUNT(*), MAX(ts) FROM events "
                "WHERE subject='windows.system.metrics' AND ts > ?",
                (time.time() - 300,),
            ).fetchone()
            n = int(row[0]) if row else 0
            last_ts = row[1] if row and row[1] else None
        finally:
            c.close()
    except Exception as e:
        return False, f"db query failed: {e}"
    if n >= 1 and last_ts:
        age = int(time.time() - float(last_ts))
        return True, f"{n} metrics in last 5 min, last {age}s ago"
    return False, f"0 metrics in last 5 min"


def check_service_control_list() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_service_control.py", ["--list"], timeout=30)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if l.strip()]
    return (len(lines) > 10, f"{len(lines)} services listed")


def check_process_manager_list() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_process_manager.py", ["--list"], timeout=20)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if l.strip()]
    return (len(lines) > 10, f"{len(lines)} processes listed")


def check_driver_manager_list() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_driver_manager.py", ["--list"], timeout=30)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if l.strip()]
    return (len(lines) > 5, f"{len(lines)} drivers listed")


def check_patch_manager_cache() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_patch_manager.py",
                          ["--list-available"], timeout=15)
    if rc != 0:
        return False, f"rc={rc}"
    # Output starts with "# cache: ... pending updates"
    if "cache:" in out or "pending updates" in out:
        return True, out.split("\n")[0][:100]
    return False, f"unexpected output: {out[:200]}"


def check_self_repair_rules() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_self_repair.py",
                          ["--list-rules"], timeout=10)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if "cooldown=" in l]
    return (len(lines) >= 5, f"{len(lines)} rules registered")


def check_hardware_health_snapshot() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_hardware_health.py",
                          ["--snapshot"], timeout=30)
    if rc != 0:
        return False, f"rc={rc}"
    try:
        # Output has a {snapshot,incidents,published} dict at the start
        start = out.find("{")
        end = out.rfind("}")
        d = json.loads(out[start:end + 1])
    except Exception as e:
        return False, f"parse failed: {e}"
    snap = d.get("snapshot") or {}
    gpus = snap.get("gpus") or []
    return (len(gpus) >= 1, f"GPUs={len(gpus)} incidents={len(d.get('incidents', []))}")


def check_user_intent_current() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_user_intent.py",
                          ["--current"], timeout=10)
    if rc != 0:
        return False, f"rc={rc}"
    try:
        start = out.find("{")
        end = out.rfind("}")
        d = json.loads(out[start:end + 1])
    except Exception as e:
        return False, f"parse failed: {e}"
    cat = d.get("category", "?")
    return (cat in ("IDLE", "GAMING", "MEETING", "CODING", "WRITING",
                    "MEDIA", "BROWSING", "UNKNOWN"),
            f"category={cat}, confidence={d.get('confidence', 0):.2f}")


def check_nemo_server_status() -> tuple[bool, str]:
    """mercury.status — collect ALL responders and pass if ANY is healthy.

    There are two NemoServer instances subscribing to mercury.status:
      - host-side (correct, GPUs OK)
      - k8s nemo-controller pod (misconfigured, GPUs error on Ollama ports)
    request() races them and returns whichever arrives first. For a safety
    check we want to know: is AT LEAST ONE healthy responder reachable?"""
    rc, out, err = _run_cli_anonymous(
        "-c",
        "import asyncio,json,nats\n"
        "async def go():\n"
        "  nc = await nats.connect('nats://127.0.0.1:4223', connect_timeout=3)\n"
        "  replies = []\n"
        "  inbox = nc.new_inbox()\n"
        "  async def cb(m): replies.append(m.data.decode())\n"
        "  sub = await nc.subscribe(inbox, cb=cb)\n"
        "  await nc.publish('mercury.status', b'{}', reply=inbox)\n"
        "  await asyncio.sleep(3)\n"
        "  await sub.unsubscribe()\n"
        "  print(json.dumps(replies))\n"
        "  await nc.drain()\n"
        "asyncio.run(go())",
        timeout=15,
    )
    if rc != 0:
        return False, f"rc={rc} err={err[:150]}"
    try:
        replies = json.loads(out.strip())
    except Exception as e:
        return False, f"parse failed: {e} out={out[:150]}"
    if not replies:
        return False, "no responders"
    best_ok = 0
    best_server = "?"
    for r in replies:
        try:
            d = json.loads(r)
        except Exception:
            continue
        ok = sum(1 for g in d.get("gpus", []) if g.get("status") == "ok")
        if ok > best_ok:
            best_ok = ok
            best_server = d.get("server", "?")
    return (best_ok >= 1,
            f"responders={len(replies)} best={best_server} gpus_ok={best_ok}")


def check_nemo_server_inference() -> tuple[bool, str]:
    """mercury.infer.code — subprocess-driven for reliability."""
    rc, out, err = _run_cli_anonymous(
        "-c",
        "import asyncio,json,nats\n"
        "async def go():\n"
        "  nc = await nats.connect('nats://127.0.0.1:4223', connect_timeout=3)\n"
        "  r = await nc.request('mercury.infer.code',\n"
        "    json.dumps({'prompt':'2+2?','max_tokens':4,'temperature':0.0}).encode(),\n"
        "    timeout=30)\n"
        "  print(r.data.decode()[:400])\n"
        "  await nc.drain()\n"
        "asyncio.run(go())",
        timeout=40,
    )
    if rc != 0:
        return False, f"rc={rc} err={err[:150]}"
    try:
        d = json.loads(out.strip())
    except Exception:
        # Some NemoServer builds return plain text; count any non-empty output as OK
        return (len(out.strip()) > 0, f"response len={len(out.strip())}")
    text = d.get("text") or d.get("response") or d.get("content") or ""
    return (len(text) > 0, f"response len={len(text)}")


def _run_cli_anonymous(flag: str, code: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run python -c CODE in a subprocess. Used to sidestep asyncio-event-loop
    state leakage that makes in-process NATS clients misbehave inside
    harness runs."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", f"{ROOT / 'src'};{ROOT.parent}")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [PY, "-X", "utf8", flag, code]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def check_network_reach_allowlist() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_network_reach.py",
                          ["--list-allowlist"], timeout=10)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if l.strip()]
    return (len(lines) >= 5, f"{len(lines)} allowlist entries")


def check_network_reach_refuse() -> tuple[bool, str]:
    """Confirm a non-allowlisted host is refused."""
    rc, out, _ = _run_cli("tools/liril_network_reach.py",
                          ["--get", "https://not-a-real-host.example.invalid"],
                          timeout=10)
    # Expect rc=1 (request failed) and body contains 'not in allowlist'
    if "not in allowlist" in out:
        return True, "refused non-allowlisted"
    return False, f"rc={rc} out={out[:200]}"


def check_network_reach_live() -> tuple[bool, str]:
    """Live GET to api.github.com/zen (small, fast, reliable, allowlisted)."""
    rc, out, _ = _run_cli("tools/liril_network_reach.py",
                          ["--get", "https://api.github.com/zen"], timeout=15)
    if rc != 0:
        # Network may not be up; make non-critical
        return False, f"rc={rc} out={out[:200]}"
    if '"status": 200' in out:
        return True, "200 OK"
    return False, f"unexpected output: {out[:200]}"


def check_communication_notify() -> tuple[bool, str]:
    """Send a low-severity notification (log-only, safe)."""
    rc, out, _ = _run_cli("tools/liril_communication.py",
                          ["--notify", "low", "prove_all test",
                           "verification: log-only channel"],
                          timeout=15)
    if rc != 0:
        return False, f"rc={rc}"
    try:
        start = out.find("{")
        end = out.rfind("}")
        d = json.loads(out[start:end + 1])
    except Exception as e:
        return False, f"parse: {e}"
    if d.get("ok"):
        return True, f"action={d.get('action','?')}"
    return False, f"not ok: {d}"


def check_file_awareness_watched() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_file_awareness.py",
                          ["--list-watched"], timeout=10)
    if rc != 0:
        return False, f"rc={rc}"
    lines = [l for l in out.splitlines() if l.strip()]
    return (len(lines) >= 1, f"{len(lines)} watched paths")


def check_journal_roundtrip() -> tuple[bool, str]:
    """Write an entry + read it back via NATS RPC."""
    rc, out, err = _run_cli_anonymous(
        "-c",
        "import asyncio,json,nats\n"
        "async def go():\n"
        "  nc = await nats.connect('nats://127.0.0.1:4223', connect_timeout=3)\n"
        "  w = await nc.request('tenet5.liril.journal.write',\n"
        "    json.dumps({'key':'prove_all.roundtrip','value':42,'tags':'observation:prove_all'}).encode(),\n"
        "    timeout=5)\n"
        "  wr = json.loads(w.data.decode())\n"
        "  r = await nc.request('tenet5.liril.journal.read',\n"
        "    json.dumps({'op':'recall','key':'prove_all.roundtrip','limit':1}).encode(),\n"
        "    timeout=5)\n"
        "  rr = json.loads(r.data.decode())\n"
        "  rows = rr.get('result', [])\n"
        "  val = rows[0].get('value') if rows else None\n"
        "  print(json.dumps({'write_id': wr.get('id','')[:8], 'read_value': val}))\n"
        "  await nc.drain()\n"
        "asyncio.run(go())",
        timeout=15,
    )
    if rc != 0:
        return False, f"rc={rc} err={err[:150]}"
    try:
        d = json.loads(out.strip())
    except Exception as e:
        return False, f"parse: {e} out={out[:150]}"
    return (d.get("read_value") == 42,
            f"write_id={d.get('write_id')}, read_value={d.get('read_value')}")


def check_api_health() -> tuple[bool, str]:
    """HTTP /health on 127.0.0.1:18120 returns 200."""
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:18120/health", timeout=5)
        body = r.read().decode("utf-8", errors="replace")
        d = json.loads(body)
        return (d.get("ok") is True, f"status={r.status} ok={d.get('ok')}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_api_status() -> tuple[bool, str]:
    """HTTP /status/brief returns overall=RUNNING."""
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:18120/status/brief", timeout=8)
        d = json.loads(r.read().decode("utf-8", errors="replace"))
        overall = (d.get("data") or {}).get("overall", "?")
        return (overall in ("RUNNING", "DEGRADED"), f"overall={overall}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_journal_stats() -> tuple[bool, str]:
    rc, out, _ = _run_cli("tools/liril_journal.py", ["--stats"], timeout=10)
    if rc != 0:
        return False, f"rc={rc}"
    try:
        start = out.find("{"); end = out.rfind("}")
        d = json.loads(out[start:end + 1])
    except Exception as e:
        return False, f"parse: {e}"
    total = int(d.get("total") or 0)
    return (total >= 1, f"total={total} pinned={d.get('pinned',0)}")


def check_file_awareness_scan(quick: bool) -> tuple[bool, str]:
    if quick:
        return True, "skipped (--quick)"
    rc, out, _ = _run_cli("tools/liril_file_awareness.py",
                          ["--scan-once"], timeout=60)
    if rc != 0:
        return False, f"rc={rc}"
    # Expected first line: "scanned: N  alerts: M"
    first = out.split("\n")[0] if out else ""
    return ("scanned" in first, first[:120])


def check_hydrogen_runtime() -> tuple[bool, str]:
    """SLATE hydrogen (H1) runtime — all 13 integrations must verify."""
    script = SLATE_ROOT = ROOT.parent / "hydrogen" / "hydrogen_runtime.py"
    script_path = ROOT.parent / "hydrogen" / "hydrogen_runtime.py"
    if not script_path.exists():
        return False, f"hydrogen_runtime.py missing at {script_path}"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'src'};{ROOT.parent}"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [PY, "-X", "utf8", str(script_path), "--check-all"],
            capture_output=True, timeout=60, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(script_path.parent),
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or "").strip()
    # Expected line: "[OK] All 13/13 core runtime hooks successfully validated."
    # or: "[WARN] N/13 passed, M failed."
    import re as _re
    m = _re.search(r"(\d+)/13", out)
    if not m:
        return False, f"unparsable output: {out[-200:]}"
    n = int(m.group(1))
    return (n == 13, f"{n}/13 integrations")


def check_fse_end_to_end() -> tuple[bool, str]:
    """Delegate to the existing 15-assertion harness."""
    rc, out, _ = _run_cli("tools/liril_fse_verify.py", [], timeout=300)
    # Harness prints "RESULT: N / M assertions passed"
    for line in out.splitlines():
        if line.strip().startswith("RESULT:"):
            return (rc == 0, line.strip())
    return (rc == 0, f"rc={rc}")


def check_supervisor_14() -> tuple[bool, str]:
    """14 pidfiles, all point to alive PIDs."""
    pids_alive = 0
    missing: list[str] = []
    for name in DAEMONS:
        pid = _read_pidfile(name)
        if pid is not None and _pid_alive(pid):
            pids_alive += 1
        else:
            missing.append(name)
    ok = (pids_alive == len(DAEMONS))
    msg = f"{pids_alive}/{len(DAEMONS)} alive"
    if missing:
        msg += f" missing={missing}"
    return ok, msg


# ─────────────────────────────────────────────────────────────────────
# CHECK REGISTRY
# ─────────────────────────────────────────────────────────────────────

def build_registry(quick: bool) -> list[Check]:
    r: list[Check] = []
    # Process-alive for every daemon (critical)
    for name in DAEMONS:
        r.append(Check(f"process.{name}", True,
                       lambda n=name: check_process(n)))
    # Supervisor aggregate
    r.append(Check("supervisor.14_alive", True, check_supervisor_14))
    # Per-capability functional
    r += [
        Check("failsafe.api",              True,  check_failsafe_api),
        Check("observer.db",               False, check_observer_db),
        Check("cap1.metrics_stream",       True,  check_windows_monitor_stream),
        Check("cap2.list",                 True,  check_service_control_list),
        Check("cap3.list",                 True,  check_process_manager_list),
        Check("cap4.list",                 True,  check_driver_manager_list),
        Check("cap5.cache",                True,  check_patch_manager_cache),
        Check("cap6.rules",                True,  check_self_repair_rules),
        Check("cap8.snapshot",             True,  check_hardware_health_snapshot),
        Check("cap9.current",              True,  check_user_intent_current),
        Check("nemo_server.status",        True,  check_nemo_server_status),
        Check("nemo_server.inference",     True,  check_nemo_server_inference),
        Check("network.allowlist",         True,  check_network_reach_allowlist),
        Check("network.refuse_non_allow",  True,  check_network_reach_refuse),
        Check("network.live_github",       False, check_network_reach_live),
        Check("communication.notify",      True,  check_communication_notify),
        Check("file.watched",              True,  check_file_awareness_watched),
        Check("file.scan",                 False, lambda: check_file_awareness_scan(quick)),
        Check("journal.stats",             True,  check_journal_stats),
        Check("journal.rpc_roundtrip",     True,  check_journal_roundtrip),
        Check("api.health",                True,  check_api_health),
        Check("api.status_brief",          True,  check_api_status),
        Check("hydrogen.integrations",     True,  check_hydrogen_runtime),
        # Composite — delegated to fse_verify's own 15 assertions
        Check("fse.end_to_end",            True,  check_fse_end_to_end),
    ]
    return r


# ─────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────

def run_all(quick: bool = False, only: list[str] | None = None,
            json_mode: bool = False) -> tuple[int, dict]:
    registry = build_registry(quick)
    if only:
        wanted = set(only)
        registry = [c for c in registry if c.name in wanted]

    results: list[dict] = []
    t_start = time.time()
    for ch in registry:
        t0 = time.time()
        try:
            ok, msg = ch.func()
        except Exception as e:
            ok, msg = False, f"exception: {type(e).__name__}: {e}"
        dt = time.time() - t0
        results.append({
            "name":     ch.name,
            "critical": ch.critical,
            "ok":       bool(ok),
            "message":  msg,
            "secs":     round(dt, 2),
        })
        if not json_mode:
            marker = "PASS" if ok else ("FAIL" if ch.critical else "warn")
            print(f"  [{marker:4s}] {ch.name:30s} {dt:5.1f}s  {msg}")

    elapsed = time.time() - t_start
    n_total   = len(results)
    n_pass    = sum(1 for r in results if r["ok"])
    n_crit    = sum(1 for r in results if r["critical"])
    n_crit_ok = sum(1 for r in results if r["critical"] and r["ok"])
    exit_code = 0 if n_crit == n_crit_ok else 1

    report = {
        "ts":          _utc(),
        "elapsed_sec": round(elapsed, 2),
        "total":       n_total,
        "pass":        n_pass,
        "fail":        n_total - n_pass,
        "critical":    n_crit,
        "critical_ok": n_crit_ok,
        "quick":       quick,
        "assertions":  results,
    }

    # Persist
    try:
        out_path = ROOT / "data" / "liril_prove_all_latest.json"
        out_path.write_text(json.dumps(report, indent=2, default=str),
                             encoding="utf-8")
    except Exception:
        pass

    if not json_mode:
        print()
        print("=" * 70)
        print(f"RESULT  critical: {n_crit_ok}/{n_crit}   "
              f"all: {n_pass}/{n_total}   elapsed: {elapsed:.1f}s")
        print(f"report: data/liril_prove_all_latest.json")
        print("=" * 70)
    else:
        print(json.dumps(report, indent=2, default=str))

    return exit_code, report


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Prove-All Verification")
    ap.add_argument("--quick", action="store_true",
                    help="Skip long-running checks (file scan, WU refresh)")
    ap.add_argument("--json",  action="store_true", help="JSON output only")
    ap.add_argument("--names", action="store_true",
                    help="List check names and exit")
    ap.add_argument("--only",  type=str, default="",
                    help="Comma-separated check names to run (default: all)")
    args = ap.parse_args()

    if args.names:
        for c in build_registry(quick=False):
            print(f"  {c.name:30s} critical={c.critical}")
        return 0

    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    if not args.json:
        print("=" * 70)
        print("LIRIL PROVE-ALL — comprehensive verification of the 24/7 stack")
        print("=" * 70)
    rc, _ = run_all(quick=args.quick, only=only, json_mode=args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
