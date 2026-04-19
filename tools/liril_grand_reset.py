"""One-shot reset. Kills all popup_hunter + posting_daemon instances,
then starts exactly one of each with the newest code. Filename is neutral
so old hunters don't match their own patterns in my cmdline."""
import os, sys, time, subprocess
from pathlib import Path
import psutil

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
PYW = SLATE / ".venv" / "Scripts" / "pythonw.exe"

# Kill ALL current popup_hunter + poster instances
killed = {"hunter": 0, "poster": 0}
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        c = " ".join(str(x) for x in (p.info.get("cmdline") or [])).lower()
        if "liril_popup_hunter" in c:
            p.kill()
            killed["hunter"] += 1
        elif "lirilclaw_web.posting_daemon" in c or ("posting_daemon.py" in c and "tenet5" in c):
            p.kill()
            killed["poster"] += 1
    except Exception:
        pass

time.sleep(2)

# Start exactly ONE popup_hunter with fresh code
DETACH = 0x00000008
NEW_GROUP = 0x00000200
flags = subprocess.CREATE_NO_WINDOW | DETACH | NEW_GROUP

subprocess.Popen(
    [str(PYW), "-X", "utf8", "tools/liril_popup_hunter.py"],
    cwd=str(SLATE),
    stdout=open(SLATE / "data" / "liril_popup_hunter.log", "a", encoding="utf-8"),
    stderr=open(SLATE / "data" / "liril_popup_hunter.err", "a", encoding="utf-8"),
    stdin=subprocess.DEVNULL,
    creationflags=flags,
    close_fds=True,
)

# DO NOT restart posting_daemon — Instagram rate-limited the account for
# ~12 hours. Restarting now would just hit the rate limit again on every
# cycle. User should wait until Instagram releases the throttle, then
# manually run start_posting.bat or this script with --restart-poster.
if "--restart-poster" in sys.argv:
    env = dict(os.environ,
               PYTHONPATH=r"E:\S.L.A.T.E\tenet5\src;E:\S.L.A.T.E;E:\S.L.A.T.E\tenet5\tools\lirilclaw",
               NATS_URL="nats://127.0.0.1:4222",
               PYTHONIOENCODING="utf-8")
    subprocess.Popen(
        [str(PYW), "-X", "utf8", "-m", "tenet.lirilclaw_web.posting_daemon",
         "--campaign", "aIx2D0QA0n2K", "--interval", "30",
         "--platforms", "instagram"],
        cwd=str(SLATE), env=env, creationflags=flags,
        stdout=open(SLATE / "data" / "posting_generator.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True,
    )
    subprocess.Popen(
        [str(PYW), "-X", "utf8", "-m", "tenet.lirilclaw_web.posting_daemon",
         "--post-outbox", "--interval", "30"],
        cwd=str(SLATE), env=env, creationflags=flags,
        stdout=open(SLATE / "data" / "posting_outbox.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True,
    )
    print(f"reset: killed_hunter={killed['hunter']} killed_poster={killed['poster']} | respawned 1 hunter + 2 posters (30-min interval)")
else:
    print(f"reset: killed_hunter={killed['hunter']} killed_poster={killed['poster']} | respawned 1 hunter, posters NOT restarted (IG rate-limited ~12h)")
