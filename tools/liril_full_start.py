"""liril_full_start — user directive: start full automation.

Since user wants full automation, the `.game_mode` gate is REMOVED and
the dev-team daemon is started. Perception stack already running.
GPU stack (llama-server 8082/8083) already up per diagnostic.
mercury.infer confirmed reachable.

Leaves these DEAD:
  - scripts/liril_instagram_poster.py — IG is rate-limited until 18:03 UTC
  - tools/lirilclaw/posting_daemon — same rate-limit reason
  - scripts/daily_mailer.py — Gmail credentials 535 Bad Credentials (user must fix)
  - tools/lirilclaw/s504_broadcaster.py — same Gmail issue

Starts:
  - tools/liril_dev_team.py --daemon --interval-min 20
    (autonomous 6-role pipeline, GPU-backed, whitelisted by popup_hunter)
"""
import os, sys, time, subprocess
from pathlib import Path
import psutil

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
PYW = SLATE / ".venv" / "Scripts" / "pythonw.exe"
GAME_MODE = SLATE / "data" / ".game_mode"

# 1. Remove the .game_mode sentinel (user asked for full automation)
removed = False
if GAME_MODE.exists():
    GAME_MODE.unlink()
    removed = True

# 2. Kill any lingering hidden IG poster that was spawned before gating
killed = 0
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        cmd = " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
        if "liril_instagram_poster" in cmd or "daily_mailer.py" in cmd:
            p.kill()
            killed += 1
    except Exception:
        pass

# 3. Start dev-team daemon if not already running
DETACH = 0x00000008
NEW_GROUP = 0x00000200
flags = subprocess.CREATE_NO_WINDOW | DETACH | NEW_GROUP

already = any(
    "liril_dev_team.py" in " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
    and "--daemon" in " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
    for p in psutil.process_iter(["cmdline"])
)

if not already:
    env = dict(os.environ,
               PYTHONPATH=str(SLATE / "src"),
               NATS_URL="nats://127.0.0.1:4223",
               PYTHONIOENCODING="utf-8")
    subprocess.Popen(
        [str(PYW), "-X", "utf8", "tools/liril_dev_team.py", "--daemon", "--interval-min", "20"],
        cwd=str(SLATE), env=env, creationflags=flags,
        stdout=open(SLATE / "data" / "liril_dev_team.log", "a", encoding="utf-8"),
        stderr=open(SLATE / "data" / "liril_dev_team.err", "a", encoding="utf-8"),
        stdin=subprocess.DEVNULL, close_fds=True,
    )

print(f"game_mode_removed={removed}  hidden_posters_killed={killed}  dev_team_already_running={already}")
