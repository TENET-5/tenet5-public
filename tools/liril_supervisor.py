#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T16:10:00Z | Author: claude_code | Change: register liril_autonomous (24/7 cron: LIRIL + TENET5 OS + Docker + website + OSINT)
"""LIRIL Supervisor — the 24/7 keep-alive daemon for the LIRIL capability stack.

Why this exists
---------------
User directive (2026-04-19): "ensure liril is 100% actively working 24 hours a day."

Each capability ships a --daemon mode (Cap#1 monitor, Cap#2 service-control
classify-all loop, Cap#3 process classify-top loop, Cap#4 driver classify-all
loop, Cap#10 failsafe level manager). But none of those daemons restart
themselves if they crash. Prior boot scripting (services/boot_liril_daemons.ps1)
used Start-Process -WindowStyle Minimized — fire-and-forget, no restart.

This supervisor runs as a single long-lived Python process that owns each
daemon's subprocess.Popen handle. Every 5 seconds it polls each child; if
.poll() returns non-None the child has exited, and the supervisor respawns
it with exponential backoff. Heartbeats go to the Cap#10 failsafe subject
every 60s so Cap#10 can auto-escalate if the supervisor itself dies.

Architecture
------------
  Registry (SUPERVISED_DAEMONS below)
    └─ per entry: name, module/script, args, critical (restart or let die)
  SupervisorLoop (async)
    ├─ On start: spawn every entry that isn't already running (pidfile check)
    ├─ Every tick: poll each handle, respawn dead ones (backoff)
    ├─ Every 60s: publish heartbeat + per-daemon status to NATS
    └─ Listens on tenet5.liril.supervisor.command for start/stop/restart

Pidfiles
--------
  data/liril_supervisor/<name>.pid stores each child's PID so a supervisor
  restart doesn't orphan survivors. If a pidfile's PID is still running and
  matches the expected command-line, the supervisor adopts it (doesn't spawn
  a duplicate). If the pidfile exists but the PID is dead, the supervisor
  clears it and spawns fresh.

Backoff
-------
  First restart: 5s
  Each subsequent restart within 60s: multiply by 2, cap 300s
  If a child restarts >= 5 times in 10 minutes → file severity=high incident
  into the Cap#10 failsafe stream and back off to 300s indefinitely until
  the cycle quiets down.

CLI
---
  --daemon                 Run as the supervisor loop (foreground)
  --status                 JSON status of every supervised daemon
  --start NAME             Spawn NAME if not running
  --stop NAME              Kill NAME (graceful TerminateProcess, then hard kill)
  --restart NAME           Stop + start
  --register-task          Register a Windows Scheduled Task that runs us at logon
  --unregister-task        Remove that scheduled task
  --show-registry          Print the supervised-daemon registry and exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT       = Path(__file__).resolve().parent.parent
VENV_PY    = ROOT / ".venv" / "Scripts" / "pythonw.exe"
if not VENV_PY.exists():
    VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PIDFILE_DIR = ROOT / "data" / "liril_supervisor"
PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG  = ROOT / "data" / "liril_supervisor.jsonl"

NATS_URL        = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
STATUS_SUBJECT  = "tenet5.liril.supervisor.status"
COMMAND_SUBJECT = "tenet5.liril.supervisor.command"
HEARTBEAT_SUBJECT = "tenet5.liril.failsafe.heartbeat.supervisor"
INCIDENT_SUBJECT  = "tenet5.liril.failsafe.incident"

POLL_INTERVAL_SEC    = 5.0
HEARTBEAT_INTERVAL   = 60.0
BACKOFF_INITIAL      = 5.0
BACKOFF_MAX          = 300.0
FLAP_WINDOW_SEC      = 600.0  # 10 min
FLAP_RESTART_MAX     = 5

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0


# ─────────────────────────────────────────────────────────────────────
# DAEMON REGISTRY
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Daemon:
    name: str                    # unique key; used for pidfile + heartbeat subject
    script: str                  # absolute or ROOT-relative path
    args: list[str] = field(default_factory=list)
    critical: bool = True        # restart on death (vs log-and-leave)
    env: dict[str, str] = field(default_factory=dict)


# These are the daemons the supervisor ensures are running.
# Ordering is boot order — Cap#10 (fail-safe) first, then observer, then caps.
SUPERVISED_DAEMONS: list[Daemon] = [
    Daemon(
        name="failsafe",
        script="tools/liril_fail_safe_escalation.py",
        args=["--daemon"],
        critical=True,
    ),
    Daemon(
        name="observer",
        script="tools/liril_observer.py",
        args=[],
        critical=True,
    ),
    Daemon(
        name="windows_monitor",
        script="tools/liril_windows_monitor.py",
        args=[],
        critical=True,
    ),
    Daemon(
        name="service_control",
        script="tools/liril_service_control.py",
        args=["--daemon"],
        critical=False,  # daemon is classify-loop only; non-fatal if missing
    ),
    Daemon(
        name="process_manager",
        script="tools/liril_process_manager.py",
        args=["--daemon", "--daemon-interval", "5", "--daemon-top", "15"],
        critical=False,
    ),
    Daemon(
        name="driver_manager",
        script="tools/liril_driver_manager.py",
        args=["--daemon", "--daemon-interval", "30"],
        critical=False,
    ),
    Daemon(
        name="hardware_health",
        script="tools/liril_hardware_health.py",
        args=["--daemon"],
        critical=True,
    ),
    Daemon(
        name="patch_manager",
        script="tools/liril_patch_manager.py",
        args=["--daemon", "--daemon-interval", "6"],
        critical=False,   # WU search is slow and infrequent; daemon-loss is non-fatal
    ),
    Daemon(
        name="self_repair",
        script="tools/liril_self_repair.py",
        args=["--daemon"],
        critical=True,     # autonomous repair is core to the 24/7 posture
    ),
    Daemon(
        name="user_intent",
        script="tools/liril_user_intent.py",
        args=["--daemon"],
        critical=True,     # observational, but we want the signal flowing 24/7
    ),
    # NemoServer — NATS → llama-server bridge for mercury.infer.code and
    # tenet5.liril.execute. The k8s nemo-controller pod is misconfigured
    # (points at banned Ollama ports 11434/11435); host-side instance with
    # correct env takes ownership of the subjects.
    Daemon(
        name="nemo_server",
        script="src/tenet/aurora/nemo_server.py",
        args=[],
        critical=True,
        env={
            "GPU0_URL": "http://127.0.0.1:8082",
            "GPU1_URL": "http://127.0.0.1:8083",
        },
    ),
    # LIRIL's three new skills (2026-04-19, from the "what do you want next" poll)
    Daemon(
        name="network_reach",
        script="tools/liril_network_reach.py",
        args=["--daemon"],
        critical=True,     # gateway — many future caps will depend on it
    ),
    Daemon(
        name="communication",
        script="tools/liril_communication.py",
        args=["--daemon"],
        critical=True,
    ),
    Daemon(
        name="file_awareness",
        script="tools/liril_file_awareness.py",
        args=["--daemon"],
        critical=True,
    ),
    Daemon(
        name="journal",
        script="tools/liril_journal.py",
        args=["--daemon"],
        critical=True,      # foundation for future cross-cap learning
    ),
    Daemon(
        name="api",
        script="tools/liril_api.py",
        args=["--serve"],
        critical=True,      # user-facing entrypoint
    ),
    Daemon(
        name="autonomous",
        script="tools/liril_autonomous.py",
        args=["--daemon"],
        critical=True,      # 24/7 cron orchestrator — LIRIL, TENET5 OS,
                            # Docker, website, OSINT all run from here
        env={
            # Run LIVE by default — CEO directive 2026-04-19. Jobs still
            # respect fse gate + intent gate + commit-rate cap.
            "LIRIL_AUTO_DRY_RUN": "0",
        },
    ),
]


# ─────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RunState:
    proc: subprocess.Popen | None = None
    pid: int | None = None
    started_at: float = 0.0
    restarts: list[float] = field(default_factory=list)  # timestamps in FLAP_WINDOW_SEC
    next_allowed_start: float = 0.0
    backoff_sec: float = BACKOFF_INITIAL
    flap_blocked: bool = False
    last_exit_code: int | None = None


# Map daemon name → RunState
_STATE: dict[str, RunState] = {d.name: RunState() for d in SUPERVISED_DAEMONS}
_DAEMON_BY_NAME: dict[str, Daemon] = {d.name: d for d in SUPERVISED_DAEMONS}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[SUPERVISOR] audit log failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# PIDFILE HELPERS
# ─────────────────────────────────────────────────────────────────────

def _pidfile(name: str) -> Path:
    return PIDFILE_DIR / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    p = _pidfile(name)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _write_pid(name: str, pid: int) -> None:
    try:
        _pidfile(name).write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def _clear_pid(name: str) -> None:
    try:
        _pidfile(name).unlink(missing_ok=True)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(int(pid))
    except ImportError:
        pass
    # Fallback: tasklist
    try:
        r = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=5, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def _pid_matches_script(pid: int, script_rel: str) -> bool:
    """Weak check that a pid belongs to the expected script — guards against
    PID reuse between supervisor crashes."""
    try:
        import psutil  # type: ignore
        p = psutil.Process(int(pid))
        cmdline = " ".join(p.cmdline())
        return script_rel.replace("\\", "/").lower() in cmdline.replace("\\", "/").lower()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# SPAWN / KILL
# ─────────────────────────────────────────────────────────────────────

def _build_cmd(d: Daemon) -> list[str]:
    script_abs = (ROOT / d.script).resolve()
    return [str(VENV_PY), "-X", "utf8", str(script_abs), *d.args]


def _spawn(d: Daemon) -> subprocess.Popen | None:
    cmd = _build_cmd(d)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH":       f"{ROOT / 'src'};{ROOT.parent}",
        "PYTHONIOENCODING": "utf-8",
        "NATS_URL":         NATS_URL,
        "SYSTEM_SEED":      "118400",
        "TENET5_WORKSPACE": str(ROOT),
        "SLATE_WORKSPACE":  str(ROOT.parent),
    })
    env.update(d.env)
    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        _write_pid(d.name, p.pid)
        _audit({"kind": "spawned", "name": d.name, "pid": p.pid,
                "ts": _utc(), "cmd": cmd})
        print(f"[SUPERVISOR] spawned {d.name} pid={p.pid}")
        return p
    except Exception as e:
        print(f"[SUPERVISOR] spawn {d.name} failed: {e!r}")
        _audit({"kind": "spawn_failed", "name": d.name,
                "ts": _utc(), "error": repr(e)})
        return None


def _terminate(name: str, grace_sec: float = 5.0) -> bool:
    st = _STATE.get(name)
    pid = (st.proc.pid if st and st.proc else None) or _read_pid(name)
    if not pid or not _pid_alive(pid):
        _clear_pid(name)
        if st:
            st.proc = None
            st.pid = None
        return True
    try:
        import psutil  # type: ignore
        try:
            p = psutil.Process(int(pid))
            p.terminate()
            try:
                p.wait(timeout=grace_sec)
            except psutil.TimeoutExpired:
                p.kill()
        except psutil.NoSuchProcess:
            pass
    except ImportError:
        try:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
    _clear_pid(name)
    if st:
        st.proc = None
        st.pid = None
    _audit({"kind": "terminated", "name": name, "pid": pid, "ts": _utc()})
    return True


def _ensure_running(d: Daemon) -> None:
    """Idempotent: if already running (by pidfile), adopt; else spawn."""
    st = _STATE[d.name]
    if st.proc is not None and st.proc.poll() is None:
        return  # still running in-process
    # Try to adopt a prior run (supervisor restart scenario)
    pid = _read_pid(d.name)
    if pid and _pid_alive(pid) and _pid_matches_script(pid, d.script):
        st.pid = pid
        st.proc = None  # adopted, no Popen handle
        st.started_at = time.time()
        print(f"[SUPERVISOR] adopted {d.name} pid={pid}")
        _audit({"kind": "adopted", "name": d.name, "pid": pid, "ts": _utc()})
        return
    if pid and not _pid_alive(pid):
        _clear_pid(d.name)
    # Backoff gate
    if time.time() < st.next_allowed_start:
        return
    if st.flap_blocked and time.time() < st.next_allowed_start:
        return
    p = _spawn(d)
    if p is not None:
        st.proc = p
        st.pid = p.pid
        st.started_at = time.time()


def _check_and_restart(d: Daemon) -> None:
    st = _STATE[d.name]
    # Owned subprocess path
    if st.proc is not None:
        rc = st.proc.poll()
        if rc is None:
            return  # running
        # Exited
        st.last_exit_code = rc
        _audit({"kind": "exited", "name": d.name,
                "pid": st.pid, "rc": rc, "ts": _utc()})
        print(f"[SUPERVISOR] {d.name} exited rc={rc} pid={st.pid}")
        st.proc = None
        st.pid = None
        _clear_pid(d.name)
        if d.critical:
            _schedule_restart(d)
        return
    # Adopted path — check pid directly
    if st.pid is not None:
        if _pid_alive(st.pid):
            return
        print(f"[SUPERVISOR] adopted {d.name} pid={st.pid} has exited")
        _audit({"kind": "adopted_exited", "name": d.name,
                "pid": st.pid, "ts": _utc()})
        st.pid = None
        _clear_pid(d.name)
        if d.critical:
            _schedule_restart(d)
        return
    # Nothing running — either we never spawned or it died during grace
    if d.critical:
        _ensure_running(d)


def _schedule_restart(d: Daemon) -> None:
    st = _STATE[d.name]
    now = time.time()
    # Track recent restarts
    st.restarts = [t for t in st.restarts if now - t < FLAP_WINDOW_SEC]
    st.restarts.append(now)
    if len(st.restarts) >= FLAP_RESTART_MAX:
        st.flap_blocked = True
        st.backoff_sec = BACKOFF_MAX
        st.next_allowed_start = now + BACKOFF_MAX
        _audit({"kind": "flap_blocked", "name": d.name,
                "restarts_10min": len(st.restarts), "ts": _utc()})
        # File a severity=high incident via local sqlite (failsafe lib)
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import liril_fail_safe_escalation as _fse  # type: ignore
            _fse.file_incident_local(
                "high", "liril_supervisor",
                f"daemon {d.name} flapping: {len(st.restarts)} restarts in {int(FLAP_WINDOW_SEC)}s",
                data={"daemon": d.name, "restarts": len(st.restarts)},
            )
        except Exception as e:
            print(f"[SUPERVISOR] file_incident_local failed: {e!r}")
        return
    st.next_allowed_start = now + st.backoff_sec
    st.backoff_sec = min(BACKOFF_MAX, st.backoff_sec * 2.0)


# ─────────────────────────────────────────────────────────────────────
# NATS HEARTBEAT + STATUS
# ─────────────────────────────────────────────────────────────────────

def _status_snapshot() -> dict:
    snap = {
        "ts":        _utc(),
        "host":      os.environ.get("COMPUTERNAME", ""),
        "daemons":   {},
        "counts":    {"running": 0, "dead": 0, "flap_blocked": 0},
    }
    for d in SUPERVISED_DAEMONS:
        st = _STATE[d.name]
        pid = st.pid
        alive = bool(pid) and _pid_alive(pid)
        if st.flap_blocked:
            snap["counts"]["flap_blocked"] += 1
        elif alive:
            snap["counts"]["running"] += 1
        else:
            snap["counts"]["dead"] += 1
        snap["daemons"][d.name] = {
            "pid":             pid,
            "alive":           alive,
            "critical":        d.critical,
            "uptime_sec":      (time.time() - st.started_at) if st.started_at else 0,
            "restarts_10min":  len(st.restarts),
            "flap_blocked":    st.flap_blocked,
            "backoff_sec":     st.backoff_sec,
            "last_exit_code":  st.last_exit_code,
            "script":          d.script,
        }
    return snap


async def _publish_heartbeat_and_status(nc) -> None:
    snap = _status_snapshot()
    try:
        await nc.publish(HEARTBEAT_SUBJECT, json.dumps({
            "ts": _utc(), "cap": "supervisor",
            "daemons": snap["counts"],
        }).encode())
    except Exception:
        pass
    try:
        await nc.publish(STATUS_SUBJECT, json.dumps(snap).encode())
    except Exception:
        pass


async def _on_command(msg, nc) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception:
        return
    cmd = (d.get("command") or "").lower()
    name = d.get("name", "")
    if cmd not in {"start", "stop", "restart", "status"}:
        return
    if cmd == "status":
        snap = _status_snapshot()
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(snap).encode())
        return
    if name not in _DAEMON_BY_NAME:
        return
    daemon = _DAEMON_BY_NAME[name]
    if cmd == "stop":
        _terminate(name)
    elif cmd == "start":
        _ensure_running(daemon)
    elif cmd == "restart":
        _terminate(name)
        _STATE[name].next_allowed_start = 0
        _STATE[name].backoff_sec = BACKOFF_INITIAL
        _STATE[name].flap_blocked = False
        _ensure_running(daemon)


# ─────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────

async def _supervise() -> None:
    # Initial boot
    print(f"[SUPERVISOR] starting — {len(SUPERVISED_DAEMONS)} daemons registered")
    for d in SUPERVISED_DAEMONS:
        if not (ROOT / d.script).exists():
            print(f"[SUPERVISOR] skip {d.name} — script {d.script} missing")
            _audit({"kind": "skipped_missing_script", "name": d.name,
                    "script": d.script, "ts": _utc()})
            continue
        _ensure_running(d)

    # Connect NATS (non-fatal if unavailable — supervisor still supervises)
    nc = None
    try:
        import nats as _nats
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
        await nc.subscribe(COMMAND_SUBJECT, cb=lambda m, n=nc: _on_command(m, n))
        print(f"[SUPERVISOR] connected to {NATS_URL}")
    except Exception as e:
        print(f"[SUPERVISOR] NATS unavailable: {e!r} — continuing without NATS")

    last_heartbeat = 0.0
    try:
        while True:
            # Poll each child
            for d in SUPERVISED_DAEMONS:
                try:
                    if not (ROOT / d.script).exists():
                        continue
                    _check_and_restart(d)
                except Exception as e:
                    print(f"[SUPERVISOR] check {d.name} raised: {e!r}")
                    _audit({"kind": "check_exception", "name": d.name,
                            "error": repr(e), "ts": _utc()})

            # Heartbeat + status
            if nc is not None and time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                try:
                    await _publish_heartbeat_and_status(nc)
                    last_heartbeat = time.time()
                except Exception as e:
                    print(f"[SUPERVISOR] heartbeat publish failed: {e!r}")
                    # Try to reconnect in next iteration
                    try: await nc.close()
                    except Exception: pass
                    nc = None
                    try:
                        import nats as _nats
                        nc = await _nats.connect(NATS_URL, connect_timeout=3)
                        await nc.subscribe(COMMAND_SUBJECT, cb=lambda m, n=nc: _on_command(m, n))
                    except Exception:
                        pass

            await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        if nc is not None:
            try: await nc.drain()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# SCHEDULED TASK (Windows logon auto-start)
# ─────────────────────────────────────────────────────────────────────

TASK_NAME = "LIRIL_Supervisor_AutoStart"


def register_task() -> int:
    """Register a Windows scheduled task that runs the supervisor at logon."""
    script_abs = (ROOT / "tools" / "liril_supervisor.py").resolve()
    action_exec = str(VENV_PY)
    action_args = f'-X utf8 "{script_abs}" --daemon'
    ps_script = (
        f"$act = New-ScheduledTaskAction -Execute '{action_exec}' "
        f"-Argument '{action_args}' -WorkingDirectory '{str(ROOT)}'; "
        "$trg = New-ScheduledTaskTrigger -AtLogOn; "
        "$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1); "
        "$prn = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Highest; "
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $act "
        "-Trigger $trg -Settings $set -Principal $prn -Force | Out-Null; "
        f"Write-Host 'registered {TASK_NAME}'"
    )
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=30,
    )
    print(r.stdout or "")
    if r.stderr:
        print("stderr:", r.stderr)
    return r.returncode


def unregister_task() -> int:
    ps_script = (
        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false "
        "-ErrorAction SilentlyContinue; "
        f"Write-Host 'unregistered {TASK_NAME}'"
    )
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=30,
    )
    print(r.stdout or "")
    return r.returncode


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL 24/7 Supervisor")
    ap.add_argument("--daemon",           action="store_true", help="Run the supervisor loop")
    ap.add_argument("--status",           action="store_true", help="Print status JSON and exit")
    ap.add_argument("--start",            type=str, metavar="NAME", help="Spawn NAME")
    ap.add_argument("--stop",             type=str, metavar="NAME", help="Kill NAME")
    ap.add_argument("--restart",          type=str, metavar="NAME", help="Stop + start NAME")
    ap.add_argument("--register-task",    action="store_true", help="Register Windows logon task")
    ap.add_argument("--unregister-task",  action="store_true", help="Remove Windows logon task")
    ap.add_argument("--show-registry",    action="store_true", help="Print registry and exit")
    args = ap.parse_args()

    if args.show_registry:
        for d in SUPERVISED_DAEMONS:
            print(json.dumps(asdict(d), indent=2))
        return 0

    if args.register_task:
        return register_task()
    if args.unregister_task:
        return unregister_task()

    if args.status:
        print(json.dumps(_status_snapshot(), indent=2, default=str))
        return 0

    if args.start:
        if args.start not in _DAEMON_BY_NAME:
            print(f"unknown daemon: {args.start}")
            return 2
        _ensure_running(_DAEMON_BY_NAME[args.start])
        return 0

    if args.stop:
        if args.stop not in _DAEMON_BY_NAME:
            print(f"unknown daemon: {args.stop}")
            return 2
        _terminate(args.stop)
        return 0

    if args.restart:
        if args.restart not in _DAEMON_BY_NAME:
            print(f"unknown daemon: {args.restart}")
            return 2
        _terminate(args.restart)
        _STATE[args.restart].next_allowed_start = 0
        _STATE[args.restart].backoff_sec = BACKOFF_INITIAL
        _STATE[args.restart].flap_blocked = False
        _ensure_running(_DAEMON_BY_NAME[args.restart])
        return 0

    if args.daemon:
        try:
            asyncio.run(_supervise())
        except KeyboardInterrupt:
            print("[SUPERVISOR] stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
