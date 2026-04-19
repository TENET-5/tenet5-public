# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
"""liril_popup_cleanup — one-shot broom to kill all TENET5-tree python
processes EXCEPT our essential perception daemons.

Run once via pythonw.exe (silent). Walks every running process, matches
command line against TENET5 path prefixes, and kills everything not on
the essential-whitelist. Logs every kill to data/liril_popup_kills.jsonl.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit(1)

SITE = Path(r"E:\TENET-5.github.io")
KILLS_LOG = SITE / "data" / "liril_popup_kills.jsonl"
THOUGHTS = SITE / "data" / "liril_thoughts.jsonl"

# Our essential perception stack + user-wanted posting daemon — keep alive.
KEEP_MARKERS = [
    "liril_sees.py", "liril_hears.py", "liril_thinks.py",
    "liril_popup_hunter.py", "liril_popup_cleanup.py",
    "nemo_server.py",
    "liril_npu_service.py",         # NPU classify/route service
    "lirilclaw_web.posting_daemon", # IG posting daemon (user-wanted)
    "posting_daemon.py",
]

# If cmdline contains ANY of these AND none of the keep markers, kill.
KILL_PATH_TRIGGERS = [
    r"e:\s.l.a.t.e\tenet5\\",
    r"e:\tenet5\\",
    "tenet.os.tenet5_runtime",
    "tenet5os.tenet5",
    "native_os_window",
    "kit-app",
    "omniverse",
    "antigravity_runtime",
    "lirilclaw",
    # "posting_daemon" removed — user wants IG posting. Whitelisted.
    "agentic_cicd",
    "liril_cicd",
    "liril_autonomous",
    "liril_guardian",
    "liril_factory",
    "liril_scheduler",
    "liril_cicd_coordinator",
    "cross_agent_bridge",
    "self_repair",
    "auto_work_loop",
    "nemoclaw",
    "atmosphere_optimizer",
    "enforce_quantum_specs",
    "repair_website_quality",
    "liril_website_checker",
    "mass_mp_scanner",
    "mp_broadcaster",
    "fiis\\run_fiis",
    "tenet5_sessions_agent",  # 2 instances observed — bouncing process
    "gmail_loop", "deadman_switch",
    "s504_broadcaster", "s504_email_loop",
    "x_posting_daemon",
    # 2026-04-18 round 2 — surfaced by second snapshot
    "automation_scheduler",
    "empirical_magic_nats",
    "liril_vibe_api",
    "gpu_dashboard",
    "sator_grid_validator",
    "instagram_autoposter",
    "dashboard_api",
    "liril_watcher",
    "sator\\boot\\watchdog", "sator/boot/watchdog",
    "audit-links",
    "reduster_dev_loop",
]

SUSPECT_NAMES = {"python.exe", "pythonw.exe", "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe"}


def should_keep(cmdline_lc: str) -> bool:
    for m in KEEP_MARKERS:
        if m.lower() in cmdline_lc:
            return True
    return False


def should_kill(cmdline_lc: str) -> str | None:
    for t in KILL_PATH_TRIGGERS:
        if t in cmdline_lc:
            return t
    return None


def append_kill(entry: dict) -> None:
    try:
        KILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with KILLS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def append_thought(entry: dict) -> None:
    try:
        THOUGHTS.parent.mkdir(parents=True, exist_ok=True)
        with THOUGHTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    killed = 0
    kept = 0
    skipped = 0
    my_pid = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = p.info
            pid = info.get("pid")
            if pid == my_pid:
                continue
            name = (info.get("name") or "").lower()
            if name not in SUSPECT_NAMES:
                skipped += 1
                continue
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(str(x) for x in cmdline_list)
            lc = cmdline.lower()
            if should_keep(lc):
                kept += 1
                continue
            trig = should_kill(lc)
            if trig is None:
                skipped += 1
                continue
            # KILL
            try:
                p.kill()
                age_s = time.time() - (info.get("create_time") or time.time())
                entry = {
                    "ts":       int(time.time()),
                    "iso":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "action":   "killed_by_cleanup",
                    "pid":      pid,
                    "name":     name,
                    "cmdline":  cmdline[:400],
                    "trigger":  trig,
                    "age_s":    round(age_s, 1),
                }
                append_kill(entry)
                killed += 1
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                pass
        except Exception:
            continue
    append_thought({
        "ts":       int(time.time()),
        "kind":     "alert",
        "source":   "popup_cleanup.sweep",
        "summary":  f"one-shot popup broom: killed={killed} kept={kept} skipped={skipped}",
        "severity": "high",
    })
    print(f"cleanup: killed={killed} kept={kept} skipped={skipped}", flush=True)


if __name__ == "__main__":
    main()
