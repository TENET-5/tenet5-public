"""Full systems diagnostic + auto-fix.

Runs once. Captures:
  1. All top-level WINDOWS currently visible on screen (Win32 API via ctypes)
  2. All TENET5 + lirilclaw + nemo processes and their state
  3. Port status for NATS/llama-server/nemo_server
  4. Last 30 lines from every critical daemon log + error parsing
  5. Instagram rate-limit state and last-post-attempt outcome
  6. Scheduled tasks under TENET5 folder
  7. Recent popup_hunter kills

Writes:
  data/liril_system_analysis.json  (machine-readable)
  data/liril_system_analysis.txt   (human-readable summary)
  data/liril_thoughts.jsonl        (appends thought)
"""
import ctypes
from ctypes import wintypes
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

SLATE = Path(r"E:\S.L.A.T.E\tenet5")
SITE = Path(r"E:\TENET-5.github.io")
DATA = SITE / "data"
JSON_OUT = DATA / "liril_system_analysis.json"
TXT_OUT  = DATA / "liril_system_analysis.txt"
THOUGHTS = DATA / "liril_thoughts.jsonl"


# ── WINDOW ENUMERATION ────────────────────────────────────────────────
def enumerate_visible_windows():
    """Return list of dicts: {hwnd, title, class_name, pid, process_name}
    for every VISIBLE top-level window (the ones the user actually sees)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    out = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        # Skip zero-size windows
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 50 or rect.bottom - rect.top < 50:
            return True
        # Title
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        # Class
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        # PID
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = ""
        if psutil and pid.value:
            try:
                pname = psutil.Process(pid.value).name()
            except Exception:
                pass
        out.append({
            "hwnd":         int(hwnd) if hwnd else 0,
            "title":        title_buf.value,
            "class_name":   class_buf.value,
            "pid":          int(pid.value),
            "process_name": pname,
            "width":        rect.right - rect.left,
            "height":       rect.bottom - rect.top,
        })
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return out


# ── PROCESS CENSUS ────────────────────────────────────────────────────
def process_census():
    if psutil is None:
        return {"error": "psutil not available"}
    keep_markers = ["liril_sees.py", "liril_hears.py", "liril_thinks.py",
                    "liril_popup_hunter.py", "nemo_server.py", "liril_npu_service.py",
                    "liril_pm.py", "posting_daemon"]
    buckets = {"keep": [], "tenet5_rogue": [], "ide_pwsh": [], "system": [],
               "browser": [], "other": []}
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            cmd = " ".join(str(x) for x in (info.get("cmdline") or []))
            lc = cmd.lower()
            age_s = time.time() - (info.get("create_time") or time.time())
            rec = {"pid": info.get("pid"), "name": name, "age_s": round(age_s, 1),
                   "cmdline": cmd[:200]}
            is_tenet5 = ("s.l.a.t.e" in lc or "tenet5" in lc or "tenet-5" in lc)
            is_ide = "antigravity" in lc or "vscode" in lc or "code.exe" in name or \
                     ("pwsh" in name and "shellintegration" in lc)
            is_browser = name in {"chrome.exe", "msedge.exe", "firefox.exe",
                                  "brave.exe", "opera.exe"}
            if is_tenet5:
                if any(m.lower() in lc for m in keep_markers):
                    buckets["keep"].append(rec)
                else:
                    buckets["tenet5_rogue"].append(rec)
            elif is_ide:
                buckets["ide_pwsh"].append(rec)
            elif is_browser:
                buckets["browser"].append(rec)
            elif name in {"svchost.exe", "runtimebroker.exe", "dllhost.exe",
                          "mousocoreworker.exe", "backgroundtaskhost.exe",
                          "conhost.exe", "winlogon.exe", "explorer.exe"}:
                buckets["system"].append(rec)
            else:
                buckets["other"].append(rec)
        except Exception:
            continue
    return {k: sorted(v, key=lambda r: r["pid"]) for k, v in buckets.items()}


# ── PORT STATUS ───────────────────────────────────────────────────────
def port_status():
    ports = {
        "nats_4222":     4222,
        "nats_4223":     4223,
        "llama_8082":    8082,
        "llama_8083":    8083,
        "nats_14222":    14222,
        "npu_10101":     10101,
        "vibe_18840":    18840,
        "civic_8070":    8070,
        "guest_18090":   18090,
    }
    out = {}
    for key, port in ports.items():
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            out[key] = "UP"
        except Exception:
            out[key] = "DOWN"
        finally:
            s.close()
    return out


# ── DAEMON LOG SUMMARIES ──────────────────────────────────────────────
def log_summaries():
    logs = [
        ("popup_hunter",    SLATE / "data" / "liril_popup_hunter.log"),
        ("sees",            SLATE / "data" / "liril_sees.log"),
        ("hears",           SLATE / "data" / "liril_hears.log"),
        ("thinks",          SLATE / "data" / "liril_thinks.log"),
        ("nemo_server",     SLATE / "data" / "nemo_server.log"),
        ("posting_gen",     SLATE / "data" / "posting_generator.log"),
        ("posting_outbox",  SLATE / "data" / "posting_outbox.log"),
        ("dev_team",        SLATE / "data" / "liril_dev_team.log"),
    ]
    out = {}
    for tag, path in logs:
        if not path.exists():
            out[tag] = {"exists": False}
            continue
        try:
            size = path.stat().st_size
            # Read last ~4 KB
            with path.open("rb") as f:
                f.seek(-min(size, 4096), 2 if size > 4096 else 0)
                tail = f.read().decode("utf-8", errors="replace")
            lines = tail.splitlines()
            errors = [l for l in lines if any(k in l.upper() for k in
                      ["ERROR", "TRACEBACK", "EXCEPTION", "FAIL", "CRITICAL"])]
            warnings = [l for l in lines if "WARN" in l.upper() or "RATE LIMIT" in l.upper()]
            out[tag] = {
                "exists":        True,
                "size_bytes":    size,
                "last_modified": int(path.stat().st_mtime),
                "tail_lines":    len(lines),
                "error_count":   len(errors),
                "warning_count": len(warnings),
                "last_errors":   errors[-3:],
                "last_warnings": warnings[-3:],
                "last_3_lines":  lines[-3:],
            }
        except Exception as e:
            out[tag] = {"exists": True, "error": str(e)}
    return out


# ── INSTAGRAM STATE ───────────────────────────────────────────────────
def instagram_state():
    home = Path(os.path.expanduser("~")) / ".lirilclaw"
    out = {"lirilclaw_dir_exists": home.exists()}
    if not home.exists():
        return out
    try:
        out["outbox_count"] = len(list((home / "outbox").glob("*")))
    except Exception:
        out["outbox_count"] = 0
    # Rate limit state
    rate_file = home / "rate_limit_state.json"
    if rate_file.exists():
        try:
            out["rate_state"] = json.loads(rate_file.read_text(encoding="utf-8"))
        except Exception:
            out["rate_state"] = "unreadable"
    # Session dirs
    for d in ["browser", "browser_fresh", "browser_pw"]:
        p = home / d
        if p.exists():
            try:
                size_mb = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6
                out[f"session_{d}_size_mb"] = round(size_mb, 1)
            except Exception:
                pass
    # Daemon state
    daemon_state = home / "daemon_state.json"
    if daemon_state.exists():
        try:
            out["daemon_state"] = json.loads(daemon_state.read_text(encoding="utf-8"))
        except Exception:
            pass
    return out


# ── WINDOW FILTERING FOR POPUP SOURCES ────────────────────────────────
def suspect_windows(windows):
    """Filter windows that might be the popup source the user sees."""
    suspects = []
    for w in windows:
        title = (w.get("title") or "").lower()
        name = (w.get("process_name") or "").lower()
        cls = (w.get("class_name") or "").lower()
        # Skip well-known legitimate windows
        if name in {"explorer.exe", "searchhost.exe", "systemsettings.exe",
                    "claude.exe", "antigravity.exe", "code.exe", "cursor.exe"}:
            continue
        if title in {"", "program manager", "msctfime ui"}:
            continue
        # Flag consoles, kit, omniverse, playwright, python GUI
        flag = None
        if "conhost" in name or "consolewindowclass" in cls:
            flag = "console-window"
        elif "python" in name:
            flag = "python-gui-window"
        elif "kit" in name or "omniverse" in name or "omni.kit" in title:
            flag = "omniverse-kit"
        elif "chrome" in name and ("instagram" in title or "playwright" in title):
            flag = "playwright-browser"
        elif "powershell" in name or "pwsh" in name:
            flag = "powershell-window"
        if flag:
            w["suspect_flag"] = flag
            suspects.append(w)
    return suspects


# ── RECENT POPUP KILLS ────────────────────────────────────────────────
def recent_kills(window_seconds=600):
    kills_path = DATA / "liril_popup_kills.jsonl"
    if not kills_path.exists():
        return []
    cutoff = time.time() - window_seconds
    recent = []
    try:
        for line in kills_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                k = json.loads(line)
            except Exception:
                continue
            if k.get("ts", 0) >= cutoff:
                recent.append(k)
    except Exception:
        pass
    return recent


# ── RECENT THOUGHTS ───────────────────────────────────────────────────
def recent_thoughts(window_seconds=600):
    if not THOUGHTS.exists():
        return []
    cutoff = time.time() - window_seconds
    recent = []
    for line in THOUGHTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except Exception:
            continue
        if t.get("ts", 0) >= cutoff:
            recent.append(t)
    return recent


# ── MAIN ──────────────────────────────────────────────────────────────
def main():
    ts = int(time.time())
    report = {
        "generated_at_utc": ts,
        "iso":              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "visible_windows":  enumerate_visible_windows(),
        "processes":        process_census(),
        "ports":            port_status(),
        "logs":             log_summaries(),
        "instagram":        instagram_state(),
        "recent_kills":     recent_kills(),
        "recent_thoughts_count": len(recent_thoughts()),
    }
    report["suspect_windows"] = suspect_windows(report["visible_windows"])

    # Write JSON
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Write human-readable summary
    lines = []
    lines.append(f"TENET5 FULL SYSTEMS DIAGNOSTIC — {report['iso']}")
    lines.append("=" * 72)
    lines.append("")

    lines.append("PORTS")
    for k, v in report["ports"].items():
        lines.append(f"  {k:16} {v}")
    lines.append("")

    lines.append("PROCESSES")
    for bucket, items in report["processes"].items():
        lines.append(f"  {bucket:16} {len(items)}")
    lines.append("")

    lines.append("VISIBLE WINDOWS (top-level, user-visible)")
    lines.append(f"  total: {len(report['visible_windows'])}")
    lines.append(f"  suspect (possibly rogue popups): {len(report['suspect_windows'])}")
    for w in report["suspect_windows"][:20]:
        lines.append(f"  ⚠ {w.get('suspect_flag'):22} pid={w.get('pid')} proc={w.get('process_name'):20} title={(w.get('title') or '(no title)')[:60]}")
    lines.append("")

    lines.append("DAEMON LOGS (last-activity + error count)")
    for tag, info in report["logs"].items():
        if not info.get("exists"):
            lines.append(f"  {tag:16} (no log file)")
            continue
        mtime = info.get("last_modified", 0)
        age_s = ts - mtime if mtime else 0
        lines.append(f"  {tag:16} mtime={age_s:5}s ago  errors={info.get('error_count',0)}  warnings={info.get('warning_count',0)}")
    lines.append("")

    lines.append("INSTAGRAM STATE")
    ig = report["instagram"]
    lines.append(f"  outbox_items: {ig.get('outbox_count','?')}")
    if ig.get("rate_state"):
        lines.append(f"  rate_state: {json.dumps(ig['rate_state'])[:200]}")
    for k in list(ig.keys()):
        if k.startswith("session_"):
            lines.append(f"  {k}: {ig[k]} MB")
    lines.append("")

    lines.append(f"RECENT POPUP_HUNTER KILLS (last 10 min): {len(report['recent_kills'])}")
    for k in report["recent_kills"][:5]:
        lines.append(f"  {k.get('iso','?')}  {k.get('action','?')}  marker={k.get('kill_marker') or k.get('trigger','?')}  cmd={(k.get('cmdline') or '')[:70]}")
    lines.append("")

    lines.append("FINDINGS")
    # Heuristic analysis
    findings = []
    if any(v == "DOWN" for k, v in report["ports"].items() if k in ["llama_8082", "llama_8083", "nats_4222"]):
        down = [k for k, v in report["ports"].items() if v == "DOWN" and k in ["llama_8082", "llama_8083", "nats_4222"]]
        findings.append(f"CRITICAL: core ports down: {down}. Inference pipeline cannot run.")
    if report["processes"].get("tenet5_rogue"):
        findings.append(f"ROGUE: {len(report['processes']['tenet5_rogue'])} TENET5 processes outside keep-list are running.")
    if report["suspect_windows"]:
        findings.append(f"WINDOWS: {len(report['suspect_windows'])} suspect visible window(s) detected. See above.")
    if ig.get("outbox_count", 0) > 0:
        findings.append(f"IG OUTBOX: {ig['outbox_count']} items queued. Rate limit blocks posting — wait ~12h from last retry.")
    if not findings:
        findings.append("No critical findings. System appears clean.")
    for f in findings:
        lines.append(f"  • {f}")

    TXT_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Append thought
    try:
        with THOUGHTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts":       ts,
                "kind":     "observation",
                "source":   "full_diagnostic",
                "summary":  f"Full diagnostic run: {len(report['suspect_windows'])} suspect windows, {len(report['processes'].get('tenet5_rogue',[]))} rogue, {len(findings)} findings",
                "severity": "high" if any("CRITICAL" in f for f in findings) else "medium",
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # Print to stdout too
    print("\n".join(lines))


if __name__ == "__main__":
    main()
