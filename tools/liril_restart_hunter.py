"""Kill any currently-running popup_hunter and restart it with fresh code."""
import os, sys, time, subprocess
from pathlib import Path
try:
    import psutil
except ImportError:
    sys.exit(1)

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
PY = SLATE / ".venv" / "Scripts" / "pythonw.exe"

# Kill existing hunter(s)
killed = 0
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        cmd = " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
        if "liril_popup_hunter.py" in cmd and p.info.get("pid") != os.getpid():
            p.kill()
            killed += 1
    except Exception:
        pass

time.sleep(1)

# Spawn fresh hunter with new code — DETACHED so it survives our exit
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
creationflags = (subprocess.CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                 if sys.platform == "win32" else 0)

logf = SLATE / "data" / "liril_popup_hunter.log"
errf = SLATE / "data" / "liril_popup_hunter.err"

subprocess.Popen(
    [str(PY), "-X", "utf8", "tools/liril_popup_hunter.py"],
    cwd=str(SLATE),
    stdout=open(logf, "a", encoding="utf-8"),
    stderr=open(errf, "a", encoding="utf-8"),
    stdin=subprocess.DEVNULL,
    creationflags=creationflags,
    close_fds=True,
)

print(f"restart_hunter: killed={killed}, respawned new popup_hunter (detached, no window)")
