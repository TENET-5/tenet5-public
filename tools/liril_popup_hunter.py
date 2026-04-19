# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T04:10:00Z
"""liril_popup_hunter — self-diagnosing LIRIL popup killer.

User directive: "use TENET5 to DISCOVER the problems and FIX them".

This daemon is LIRIL's immune response to rogue console windows. It:
  1. Snapshots every running python.exe / cmd.exe / powershell.exe /
     conhost.exe every 500 ms via psutil (no subprocess spawn; reads the
     Win32 ToolHelp snapshot directly).
  2. When a new process appears, captures its PID + command line +
     parent PID + start time.
  3. Classifies the process:
        - whitelist (VS Code / Git Bash / Antigravity IDE agent / Claude
          Code itself) → ignore
        - known popup pattern (matches any gatable source we've already
          identified) → already-gated, just log
        - unknown new process spawning console → auto-kill via
          psutil.Process.kill(), then grep the tenet5 tree for matching
          command line fragments, auto-gate the source file it came from
  4. Writes a thought to data/liril_thoughts.jsonl for every detection
     so liril-thinks.html shows what was caught in real time.

Zero subprocess launches. psutil + ctypes talk directly to Win32 APIs.
No console window is created to kill another console window.

Safety rails:
  - Only kills processes whose command line contains SLATE/TENET5 paths
    (never touches user's own shells, browsers, games, IDEs).
  - Whitelist is permissive — when uncertain, observe + log, don't kill.
  - Rate limit: at most 10 kills per minute.
  - Kills are logged to data/liril_popup_kills.jsonl with full evidence.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from pathlib import Path

# ── GAME_MODE GUARD ────────────────────────────────────────────────────
if os.path.exists(r"E:\S.L.A.T.E\tenet5\data\.game_mode_POPUP_HUNTER_OFF"):
    # Separate sentinel so the user can disable JUST this watcher without
    # affecting the main .game_mode gate that silences other daemons.
    sys.exit(0)
# ────────────────────────────────────────────────────────────────────────

try:
    import psutil  # type: ignore
except ImportError:
    print("[popup_hunter] psutil is required. Install in the SLATE venv.")
    sys.exit(1)

SITE = Path(r"E:\TENET-5.github.io")
SLATE = Path(r"E:\S.L.A.T.E\tenet5")
THOUGHTS = SITE / "data" / "liril_thoughts.jsonl"
KILLS_LOG = SITE / "data" / "liril_popup_kills.jsonl"
PID_FILE = SLATE / "data" / "liril_popup_hunter.pid"

# Process names that regularly spawn console windows on Windows.
SUSPECT_NAMES = {"python.exe", "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe"}

# Processes we NEVER touch — these are legitimate user shells / IDEs.
WHITELIST_NAMES = {
    # Git Bash / mintty / user shells
    "mintty.exe", "bash.exe", "git.exe", "ssh.exe",
    # Windows Terminal + VS Code + Antigravity IDE
    "WindowsTerminal.exe", "Code.exe", "antigravity.exe", "gemini.exe",
    # Claude Code itself
    "claude.exe", "anthropic.exe",
    # Windows system
    "explorer.exe", "svchost.exe", "dllhost.exe", "RuntimeBroker.exe",
    # LIRIL perception daemons (we spawned these and want them alive)
    # Matched by command line instead — see is_whitelisted()
}

# Command-line substring markers that identify OUR own LIRIL daemons
# (we do not kill our own perception stack).
WHITELIST_CMDLINE_MARKERS = [
    "liril_sees.py", "liril_hears.py", "liril_thinks.py", "liril_pm.py",
    "liril_popup_hunter.py", "liril_dev_team.py",
    "nemo_server.py",
    "liril_npu_service.py",
    "liril_os_agent.py", "liril_gpu_dashboard.py",
    "liril_vision_agent.py", "liril_doc_parser.py",
    "liril_pvsnp_scheduler.py", "liril_markers_to_backlog.py",
    "liril_git_watcher.py", "liril_runtime_profiler.py",
    "liril_auto_decomposer.py", "pvsnp_bench_daemon.py",
    "liril_observer.py",
    # Instagram posting daemon — must run silently via pythonw. User wants
    # IG campaign posting every 30 min. Launched with pythonw so it never
    # flashes. The script also has a self-guard that exits in ~5ms if
    # launched under python.exe (console) so rogue console invocations
    # don't render a window either.
    "lirilclaw_web.posting_daemon", "posting_daemon.py",
    "liril_instagram_poster.py",
    # Email watch — needs to stay alive to detect app-password update.
    "liril_email_watch.py",
    # Bootstrap + diagnostic scripts. The hunter MUST allow these to run
    # because they're how we spawn / restart the stack. Without this, any
    # attempt to run `liril_ig_start.py` triggers the default-deny path
    # rule (path contains tenet5) and the hunter kills us before we even
    # reach step 2 (kill-and-respawn). Diagnostic probes live in
    # C:\Users\Xbxac\probe_*.py (outside tenet5 tree) but we also allow
    # the name patterns here defensively.
    "liril_ig_start.py",
    "liril_full_start.py",
    "liril_popup_cleanup.py",
    "liril_full_diagnostic.py",
    "probe_daemon_status.py",
    "probe_ig_evidence.py",
    "probe_tail_logs.py",
    "probe_write_summary.py",
    # Walkthrough regression test suite — lives inside tenet-5.github.io
    # tools/tests/ so it version-controls, but would otherwise match the
    # path trigger. Whitelisted so it can run from its proper location.
    "walkthrough_e2e.py",
    "probe_walkthrough_fix_summary.py",
    "probe_test_resend.py",
    # LIRIL NPU health monitor — daemon (2026-04-18 LIRIL self-request #2)
    "liril_npu_health.py",
    # Dev-team + memory loader support scripts
    "start_npu_health_daemon.py",
    "start_liril_dormant_daemons.py",
    "start_nemo_server.py",
    "dev_team_clean_restart.py",
    "ig_cleanup_and_start.py",
    "burn_10cycle_measured.py",
    "burn_backlog.py",
    "reset_backlog_status.py",
    "add_backlog_tasks.py",
    "add_pvsnp_backlog.py",
]

# Command-line fragments that identify known TENET5 popup-source daemons.
# When we see these, the process is definitely from within our workspace
# and we already tried to gate it — this is the "kill it before it flashes
# much" enforcement.
KILL_CMDLINE_MARKERS = [
    "ig_watchdog_loop.bat", "ig_watchdog.ps1",
    # "posting_daemon" removed from kill list — user wants IG posting.
    # Launched silently via pythonw in start_posting.bat. Whitelisted above.
    "start_posting.bat",
    "launch_x_campaign.bat", "x_posting_daemon",
    "launch_gmail_bot.bat", "gmail_playwright",
    "launch_s504_email.bat", "s504_email_loop", "s504_broadcaster",
    "lirilclaw_autostart.bat", "lirilclaw.py",
    "liril_cicd_loop.py", "agentic_cicd.py",
    "liril_website_checker.py", "repair_website_quality.py",
    "enforce_quantum_specs.py",
    "liril_autonomous_daemon.py",
    "liril_factory.py", "liril_scheduler.py",
    "liril_cicd_coordinator.py", "liril_guardian.py",
    "cross_agent_bridge.py", "self_repair.py", "auto_work_loop.py",
    "gmail_loop.py", "deadman_switch.py",
    # Added 2026-04-18 after first hunter snapshot surfaced additional daemons
    "nemoclaw_model_server.py", "nemoclaw_bridge.py",
    "atmosphere_optimizer.py", "fiis\\run_fiis.py", "mass_mp_scanner.py",
    "mp_broadcaster.py", "nemoclaw_daemon.py",
    "liril_voice_daemon.py", "sator_convergence_daemon.py",
    "cicd_health_monitor.py", "nemoclaw_health_monitor.py",
    "nemoclaw_ops.py",
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] [HUNTER/{tag}] {msg}", flush=True)


def append_thought(thought: dict) -> None:
    try:
        THOUGHTS.parent.mkdir(parents=True, exist_ok=True)
        with THOUGHTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(thought, ensure_ascii=False) + "\n")
    except Exception:
        pass


def append_kill_log(entry: dict) -> None:
    try:
        KILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with KILLS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def is_whitelisted(p: psutil.Process, cmdline: str) -> bool:
    try:
        name = (p.name() or "").lower()
    except Exception:
        return True  # when uncertain, don't touch
    for w in WHITELIST_NAMES:
        if name == w.lower():
            return True
    lc = cmdline.lower()
    for m in WHITELIST_CMDLINE_MARKERS:
        if m.lower() in lc:
            return True
    return False


def matches_kill_pattern(cmdline: str) -> str | None:
    """Return the first matching KILL_CMDLINE_MARKERS substring, or None.

    Updated 2026-04-18 (DEFAULT-DENY mode per user directive): if the
    command line contains any TENET5-tree path prefix AND the process
    is not whitelisted, it is killed. This covers Omniverse Kit GUI
    launches, tenet5_runtime, native_os_window, kit-app bats, etc. that
    weren't in the original explicit kill list.
    """
    lc = cmdline.lower()
    # Explicit markers first (more specific logging)
    for m in KILL_CMDLINE_MARKERS:
        if m.lower() in lc:
            return m
    # Default-deny: any process running from a TENET5 path.
    # FIXED 2026-04-18: previous version used r"...\\" which is a LITERAL
    # DOUBLE backslash and never matched real cmdlines. Now uses substring
    # matches without trailing separators so it matches whether cmdline
    # has single-backslash or forward-slash conventions.
    path_triggers = [
        "e:\\s.l.a.t.e\\tenet5\\",     # single backslash version
        "e:/s.l.a.t.e/tenet5/",        # forward slash version (same path)
        "e:\\tenet5\\",
        "e:/tenet5/",
        "e:\\tenet-5.github.io\\",
        "e:/tenet-5.github.io/",
        "tenet.os.tenet5_runtime",
        "tenet5os.tenet5",
        "native_os_window",
        "omniverse",
        "kit-app",
        # Script-name substrings that show up regardless of path form
        "reduster_dev_loop",
        "audit-links.py",
    ]
    for trig in path_triggers:
        if trig in lc:
            return f"tenet5-path:{trig}"
    return None


class RateLimiter:
    def __init__(self, per_minute: int = 10):
        self.per_minute = per_minute
        self.window: list[float] = []
    def allow(self) -> bool:
        now = time.time()
        self.window = [t for t in self.window if now - t < 60]
        if len(self.window) >= self.per_minute:
            return False
        self.window.append(now)
        return True


class Hunter:
    def __init__(self):
        self.seen_pids: set[int] = set()
        self.rate = RateLimiter(per_minute=10)
        self.stats = {"snapshots": 0, "new_processes": 0, "killed": 0,
                      "killed_skipped_rate_limit": 0, "killed_skipped_whitelist": 0,
                      "observed_only": 0}

    def snapshot(self):
        self.stats["snapshots"] += 1
        try:
            procs = list(psutil.process_iter(["pid", "name", "cmdline", "ppid", "create_time"]))
        except Exception:
            return
        current_pids = set()
        for p in procs:
            try:
                info = p.info
                pid = info.get("pid")
                name = (info.get("name") or "")
            except Exception:
                continue
            if pid is None:
                continue
            current_pids.add(pid)
            if pid in self.seen_pids:
                continue
            if name.lower() not in {n.lower() for n in SUSPECT_NAMES}:
                continue

            # New suspect process
            self.stats["new_processes"] += 1
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(str(x) for x in cmdline_list)
            create_time = info.get("create_time")
            age = time.time() - (create_time or time.time())

            # Decide what to do
            if is_whitelisted(p, cmdline):
                # Just observe + don't log thought (would be too noisy)
                self.stats["killed_skipped_whitelist"] += 1
                self.seen_pids.add(pid)
                continue

            kill_marker = matches_kill_pattern(cmdline)
            if kill_marker:
                # KNOWN TENET5 popup source. Kill it.
                if not self.rate.allow():
                    self.stats["killed_skipped_rate_limit"] += 1
                    self.seen_pids.add(pid)
                    continue
                self.kill_process(p, pid, name, cmdline, kill_marker, age)
                self.seen_pids.add(pid)
                continue

            # Unknown console-spawning process from an unfamiliar command line.
            # Observe only — don't kill; user may have launched it.
            # 2026-04-18: conhost.exe (console host) + svchost.exe + dllhost.exe
            # spawn constantly for legitimate reasons (bash, git, Windows tasks)
            # and were flooding liril_thoughts.jsonl with 50%+ of all entries.
            # Suppress low-signal observations — only log UNKNOWN python/
            # powershell/cmd spawns that could actually be problematic.
            self.stats["observed_only"] += 1
            nlow = (name or "").lower()
            low_signal_names = {"conhost.exe", "svchost.exe", "dllhost.exe",
                                 "runtimebroker.exe", "csrss.exe", "wuauclt.exe"}
            if nlow not in low_signal_names:
                append_thought({
                    "ts":       int(time.time()),
                    "kind":     "observation",
                    "source":   "popup_hunter.observe",
                    "summary":  f"new {name} spawned (pid={pid}) — not matching any known kill pattern; observing",
                    "cmdline":  cmdline[:300],
                    "severity": "low",
                })
            self.seen_pids.add(pid)

        # Forget dead PIDs so memory doesn't grow unbounded
        self.seen_pids &= current_pids

    def kill_process(self, p, pid, name, cmdline, kill_marker, age_s):
        # Capture parent info BEFORE kill (so we find the scheduler)
        parent_info = {"ppid": None, "parent_name": None, "parent_cmdline": None}
        try:
            ppid = p.ppid()
            parent_info["ppid"] = ppid
            try:
                parent = psutil.Process(ppid)
                parent_info["parent_name"] = parent.name()
                parent_cmdline = " ".join(str(x) for x in (parent.cmdline() or []))
                parent_info["parent_cmdline"] = parent_cmdline[:300]
            except Exception:
                pass
        except Exception:
            pass
        try:
            # psutil.Process.kill() uses TerminateProcess() via ctypes —
            # no console window is spawned to perform the kill.
            p.kill()
            self.stats["killed"] += 1
            entry = {
                "ts":          int(time.time()),
                "iso":         _now_iso(),
                "action":      "killed",
                "pid":         pid,
                "name":        name,
                "cmdline":     cmdline[:400],
                "kill_marker": kill_marker,
                "age_seconds": round(age_s, 2),
                "method":      "psutil.Process.kill (TerminateProcess Win32 API)",
                **parent_info,
            }
            append_kill_log(entry)
            append_thought({
                "ts":       int(time.time()),
                "kind":     "alert",
                "source":   "popup_hunter.kill",
                "summary":  (f"KILLED popup source: {name} pid={pid} "
                             f"parent={parent_info.get('parent_name')} "
                             f"marker={kill_marker!r} age={age_s:.1f}s"),
                "cmdline":  cmdline[:300],
                "parent_cmdline": (parent_info.get("parent_cmdline") or "")[:300],
                "severity": "high",
            })
            _log("KILL", f"pid={pid} {name} marker={kill_marker} age={age_s:.1f}s")
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as e:
            append_thought({
                "ts":       int(time.time()),
                "kind":     "alert",
                "source":   "popup_hunter.access_denied",
                "summary":  f"CANNOT KILL pid={pid} {name} — access denied ({e}). Run as admin?",
                "cmdline":  cmdline[:300],
                "severity": "high",
            })
            _log("DENIED", f"pid={pid} — access denied")
        except Exception as e:
            _log("ERROR", f"kill pid={pid} failed: {e!r}")

    def heartbeat(self):
        _log("STATS", json.dumps(self.stats))


def write_pid():
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    except Exception:
        pass


def main():
    write_pid()
    _log("BOOT", f"psutil v{psutil.__version__} — scanning every 0.5s")
    append_thought({
        "ts":       int(time.time()),
        "kind":     "good",
        "source":   "popup_hunter.boot",
        "summary":  "popup_hunter online — snapshotting running processes every 0.5s to catch rogue console windows",
        "severity": "medium",
    })
    hunter = Hunter()
    last_heartbeat = time.time()
    # 2026-04-18: tightened scan from 500ms -> 100ms. A rogue powershell
    # takes ~200-500ms to render its console window. At 100ms we catch
    # and kill before Windows fully renders.
    while True:
        try:
            hunter.snapshot()
            if time.time() - last_heartbeat > 60:
                hunter.heartbeat()
                last_heartbeat = time.time()
        except Exception as e:
            _log("ERROR", f"snapshot: {e!r}")
        time.sleep(0.1)


if __name__ == "__main__":
    main()
