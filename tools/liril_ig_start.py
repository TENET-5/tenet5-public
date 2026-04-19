"""Start ONE headless IG poster instance (pythonw) + restart hunter with
updated whitelist. Kills any stale python.exe console instances first.
Internal 30-min cycle handled inside the script itself."""
import os, subprocess, sys, time
from pathlib import Path
import psutil

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
SITE = Path(r"E:\TENET-5.github.io")
PYW = SLATE / ".venv" / "Scripts" / "pythonw.exe"
SCRIPT = SITE / "scripts" / "liril_instagram_poster.py"

# 1. Kill ALL instances (console + pythonw) — we always want a fresh spawn
#    so updated script code is actually loaded. Previous version skipped
#    respawn when pythonw was already alive, which meant deploying new code
#    required a manual kill.
killed_console = 0
killed_pythonw = 0
for p in psutil.process_iter(["pid", "name", "cmdline"]):
    try:
        name = (p.info.get("name") or "").lower()
        cmd = " ".join(str(x) for x in (p.info.get("cmdline") or []))
        if "liril_instagram_poster" in cmd.lower():
            try:
                p.kill()
                if name == "pythonw.exe":
                    killed_pythonw += 1
                else:
                    killed_console += 1
            except Exception:
                pass
    except Exception:
        pass

# 2. Also kill the popup_hunter so we can restart it with the updated whitelist
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        cmd = " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
        if "liril_popup_hunter.py" in cmd:
            p.kill()
    except Exception:
        pass

time.sleep(1.5)

# 3. Spawn popup_hunter (fresh code)
DETACH = 0x00000008
NEW_GROUP = 0x00000200
flags = subprocess.CREATE_NO_WINDOW | DETACH | NEW_GROUP

subprocess.Popen(
    [str(PYW), "-X", "utf8", "tools/liril_popup_hunter.py"],
    cwd=str(SLATE), creationflags=flags,
    stdout=open(SLATE / "data" / "liril_popup_hunter.log", "a", encoding="utf-8"),
    stderr=open(SLATE / "data" / "liril_popup_hunter.err", "a", encoding="utf-8"),
    stdin=subprocess.DEVNULL, close_fds=True,
)

# 4. Start ONE fresh pythonw instance of the IG poster
env = dict(os.environ,
           IG_USERNAME=os.environ.get("IG_USERNAME", "tenet5_osint"),
           # Password MUST be supplied via env at launch time; not stored.
           IG_PASSWORD=os.environ.get("IG_PASSWORD", ""),
           PYTHONIOENCODING="utf-8",
           PYTHONUNBUFFERED="1")  # flush prints immediately
subprocess.Popen(
    [str(PYW), "-X", "utf8", "-u", str(SCRIPT)],  # -u = unbuffered stdout
    cwd=str(SITE), env=env, creationflags=flags,
    stdout=open(SITE / "data" / "liril_instagram_poster.log", "a", encoding="utf-8"),
    stderr=open(SITE / "data" / "liril_instagram_poster.err", "a", encoding="utf-8"),
    stdin=subprocess.DEVNULL, close_fds=True,
)
spawned = True

print(f"killed_console={killed_console} killed_pythonw={killed_pythonw} spawned_new={spawned}")
