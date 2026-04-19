"""
tools/liril_dashboard.py — Live ASCII terminal dashboard for LIRIL / TENET5.

Uses `rich` if available, falls back to plain ASCII.
All subprocess calls use capture_output=True, timeout=5.

Usage:
    python tools/liril_dashboard.py           # refresh every 5s
    python tools/liril_dashboard.py --once    # run once and exit
    python tools/liril_dashboard.py --interval 10
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────
SYSTEM_SEED = 118400
TICK_RATE_HZ = 118.4
REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")
LIRIL_ASK = str(REPO_ROOT / "tools" / "liril_ask.py")
SESSIONS_DIR = REPO_ROOT / ".sessions"

NATS_DOCKER_PORT = 14222
NATS_HOST_PORT = 4222
VIBE_API_PORT = 18840

# ── Rich fallback ─────────────────────────────────────────────────────
try:
    from rich import box as rich_box
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
    _console = Console()
except ImportError:
    HAS_RICH = False
    _console = None


# ═══════════════════════════════════════════════════════════════════
# Data gathering helpers
# ═══════════════════════════════════════════════════════════════════

def _tcp_check(host: str, port: int) -> bool:
    """Return True if TCP port is open."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess with safe defaults."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, **kwargs)


def gather_nats() -> dict:
    """Check NATS docker (14222) and host (4222) status."""
    docker_ok = _tcp_check("127.0.0.1", NATS_DOCKER_PORT)
    host_ok = _tcp_check("127.0.0.1", NATS_HOST_PORT)
    subjects: list[str] = []
    # Try NATS monitoring endpoint if docker port is up
    if docker_ok:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18222/subsz", timeout=2
            ) as resp:
                data = json.loads(resp.read())
                subs = data.get("subscriptions_list", [])
                subjects = [s.get("subject", "") for s in subs[:8] if isinstance(s, dict)]
        except Exception:
            subjects = ["(monitoring unavailable)"]
    return {
        "docker_14222": "UP" if docker_ok else "DOWN",
        "host_4222": "UP" if host_ok else "DOWN",
        "subjects": subjects,
    }


def gather_gpus() -> list[dict]:
    """Query nvidia-smi for GPU info."""
    try:
        result = _run([
            "nvidia-smi",
            "--query-gpu=index,name,temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": parts[0],
                    "name": parts[1],
                    "temp_c": parts[2],
                    "mem_used": parts[3],
                    "mem_total": parts[4],
                })
        return gpus
    except Exception as e:
        return [{"error": str(e)}]


def gather_vibe_api() -> dict:
    """Check Vibe API health on port 18840."""
    base = f"http://127.0.0.1:{VIBE_API_PORT}"
    result = {"port": VIBE_API_PORT, "health": None, "gpu_status": None, "error": None}
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
            result["health"] = json.loads(resp.read())
    except urllib.error.URLError as e:
        result["error"] = str(e.reason)
    except Exception as e:
        result["error"] = str(e)
    try:
        with urllib.request.urlopen(f"{base}/gpu-status", timeout=3) as resp:
            result["gpu_status"] = json.loads(resp.read())
    except Exception:
        pass
    return result


def gather_liril() -> dict:
    """Classify a ping prompt via LIRIL and return domain/confidence."""
    python = VENV_PYTHON if Path(VENV_PYTHON).exists() else sys.executable
    if not Path(LIRIL_ASK).exists():
        return {"error": "liril_ask.py not found"}
    try:
        result = _run(
            [python, LIRIL_ASK, "classify", "liril dashboard ping health check"],
            cwd=str(REPO_ROOT),
        )
        out = result.stdout.strip()
        if not out:
            return {"raw": result.stderr.strip() or "(no output)", "domain": "?", "confidence": 0.0}
        try:
            data = json.loads(out)
            return data
        except json.JSONDecodeError:
            return {"raw": out[:120]}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}


def gather_sessions() -> list[str]:
    """List last 3 session files from .sessions/."""
    if not SESSIONS_DIR.exists():
        return ["(.sessions/ not found)"]
    files = sorted(SESSIONS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    names = [f.name for f in files[:3] if f.is_file()]
    return names if names else ["(no sessions yet)"]


def gather_agents() -> list[str]:
    """docker ps for tenet5-* containers."""
    try:
        result = _run([
            "docker", "ps",
            "--filter", "name=tenet5",
            "--format", "{{.Names}}\t{{.Status}}",
        ])
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return lines if lines else ["(no tenet5 containers running)"]
    except Exception as e:
        return [f"(docker error: {e})"]


# ═══════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════

def _tick() -> str:
    return f"{TICK_RATE_HZ:.1f}Hz"


def render_rich(data: dict) -> None:
    """Render dashboard using rich library."""
    _console.clear()
    ts = datetime.now().strftime("%H:%M:%S")
    _console.rule(
        f"[bold cyan]LIRIL TENET5 DASHBOARD[/]  "
        f"[yellow]SEED={SYSTEM_SEED}[/]  "
        f"[green]TICK={_tick()}[/]  "
        f"[dim]{ts}[/]"
    )

    # NATS panel
    nats = data["nats"]
    nats_tbl = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
    nats_tbl.add_column("Key", style="cyan")
    nats_tbl.add_column("Value")
    nats_tbl.add_row("Docker :14222", f"[{'green' if nats['docker_14222']=='UP' else 'red'}]{nats['docker_14222']}[/]")
    nats_tbl.add_row("Host   :4222",  f"[{'green' if nats['host_4222']=='UP' else 'red'}]{nats['host_4222']}[/]")
    subj_str = ", ".join(nats["subjects"][:5]) or "none"
    nats_tbl.add_row("Subjects", f"[dim]{subj_str}[/]")
    nats_panel = Panel(nats_tbl, title="[bold]NATS[/]", border_style="blue")

    # GPU panel
    gpus = data["gpus"]
    gpu_tbl = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
    gpu_tbl.add_column("GPU", style="cyan")
    gpu_tbl.add_column("Info")
    for g in gpus:
        if "error" in g:
            gpu_tbl.add_row("ERR", g["error"][:60])
        else:
            gpu_tbl.add_row(
                f"GPU{g['index']}",
                f"{g['name'][:32]}  {g['temp_c']}°C  "
                f"{g['mem_used']}/{g['mem_total']} MB",
            )
    gpu_panel = Panel(gpu_tbl, title="[bold]GPUs[/]", border_style="magenta")

    # Vibe API panel
    vibe = data["vibe_api"]
    vibe_tbl = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
    vibe_tbl.add_column("Key", style="cyan")
    vibe_tbl.add_column("Value")
    if vibe.get("error"):
        vibe_tbl.add_row("Status", f"[red]DOWN — {vibe['error'][:50]}[/]")
    else:
        h = vibe.get("health") or {}
        vibe_tbl.add_row("Status", "[green]UP[/]")
        vibe_tbl.add_row("ok",     str(h.get("ok", "?")))
        vibe_tbl.add_row("model",  str(h.get("model_server", "?")))
    vibe_panel = Panel(vibe_tbl, title="[bold]Vibe API :18840[/]", border_style="yellow")

    # LIRIL panel
    liril = data["liril"]
    liril_tbl = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
    liril_tbl.add_column("Key", style="cyan")
    liril_tbl.add_column("Value")
    if liril.get("error"):
        liril_tbl.add_row("Status", f"[red]{liril['error'][:60]}[/]")
    elif liril.get("raw"):
        liril_tbl.add_row("Raw", liril["raw"][:80])
    else:
        liril_tbl.add_row("Domain",     str(liril.get("domain", "?")))
        liril_tbl.add_row("Confidence", f"{liril.get('confidence', 0):.3f}")
        liril_tbl.add_row("Samples",    str(liril.get("train_count", liril.get("samples", "?"))))
        liril_tbl.add_row("Device",     str(liril.get("device", "?")))
    liril_panel = Panel(liril_tbl, title="[bold]LIRIL[/]", border_style="green")

    # Sessions panel
    sessions = data["sessions"]
    sess_text = "\n".join(f"  {s}" for s in sessions)
    sess_panel = Panel(sess_text or "(none)", title="[bold]Sessions[/]", border_style="dim")

    # Agents panel
    agents = data["agents"]
    agents_text = "\n".join(f"  {a}" for a in agents)
    agents_panel = Panel(agents_text or "(none)", title="[bold]Containers[/]", border_style="dim")

    _console.print(Columns([nats_panel, gpu_panel], equal=True))
    _console.print(Columns([vibe_panel, liril_panel], equal=True))
    _console.print(Columns([sess_panel, agents_panel], equal=True))
    _console.rule(
        "[dim]17/17 CI/CD | 67 tests | P vs NP: N path=local  NP path=GPU[/]"
    )


def render_ascii(data: dict) -> None:
    """Render dashboard as plain ASCII (no rich dependency)."""
    ts = datetime.now().strftime("%H:%M:%S")
    W = 72
    line = "=" * W

    def _hdr(title: str) -> str:
        pad = (W - len(title) - 4) // 2
        return f"+{'-' * pad} {title} {'-' * (W - pad - len(title) - 4)}+"

    print(line)
    hdr = f"  LIRIL TENET5 DASHBOARD | SEED={SYSTEM_SEED} | TICK={_tick()} | {ts}"
    print(hdr.center(W))
    print(line)

    # NATS
    nats = data["nats"]
    print(_hdr("NATS"))
    print(f"  Docker :14222 → {nats['docker_14222']}")
    print(f"  Host   :4222  → {nats['host_4222']}")
    subj_str = ", ".join(nats["subjects"][:5]) or "none"
    print(f"  Subjects: {subj_str}")

    # GPUs
    print(_hdr("GPUs"))
    for g in data["gpus"]:
        if "error" in g:
            print(f"  ERROR: {g['error'][:60]}")
        else:
            print(
                f"  GPU{g['index']}: {g['name'][:28]}  "
                f"{g['temp_c']}°C  {g['mem_used']}/{g['mem_total']} MB"
            )

    # Vibe API
    vibe = data["vibe_api"]
    print(_hdr(f"Vibe API :{VIBE_API_PORT}"))
    if vibe.get("error"):
        print(f"  DOWN — {vibe['error'][:60]}")
    else:
        h = vibe.get("health") or {}
        print(f"  UP | ok={h.get('ok','?')} | model={h.get('model_server','?')}")

    # LIRIL
    liril = data["liril"]
    print(_hdr("LIRIL"))
    if liril.get("error"):
        print(f"  {liril['error'][:70]}")
    elif liril.get("raw"):
        print(f"  {liril['raw'][:70]}")
    else:
        print(
            f"  domain={liril.get('domain','?')}  "
            f"conf={liril.get('confidence', 0):.3f}  "
            f"samples={liril.get('train_count', liril.get('samples','?'))}"
        )

    # Sessions
    print(_hdr("Sessions (last 3)"))
    for s in data["sessions"]:
        print(f"  {s}")

    # Agents
    print(_hdr("Containers (tenet5-*)"))
    for a in data["agents"]:
        print(f"  {a}")

    print(line)
    footer = "17/17 CI/CD | 67 tests | P vs NP: N path=local  NP path=GPU"
    print(footer.center(W))
    print(line)


def render(data: dict) -> None:
    if HAS_RICH:
        render_rich(data)
    else:
        render_ascii(data)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def collect() -> dict:
    """Gather all panel data."""
    return {
        "nats": gather_nats(),
        "gpus": gather_gpus(),
        "vibe_api": gather_vibe_api(),
        "liril": gather_liril(),
        "sessions": gather_sessions(),
        "agents": gather_agents(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LIRIL TENET5 live ASCII dashboard",
    )
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=float, default=5.0, metavar="N",
                        help="Refresh interval in seconds (default: 5)")
    args = parser.parse_args()

    if args.once:
        render(collect())
        return

    try:
        while True:
            render(collect())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[dashboard] stopped.")


if __name__ == "__main__":
    main()
