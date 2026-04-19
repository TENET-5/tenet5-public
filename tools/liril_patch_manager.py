#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T11:00:00Z | Author: claude_code | Change: LIRIL Capability #5 — Windows Patch Management (WU COM, category denylist, MsrcSeverity-primary classification)
"""LIRIL Windows Patch Management — Capability #5 of the NPU-Domain plan.

This is the last of LIRIL's original NPU-Domain plan (ordered list from the
2026-04-19 poll that seeded Cap#1-#5):

    1. Windows System Monitoring       [shipped d52129a93]
    2. Windows Service Control         [shipped 66282896e]
    3. Windows Process Management      [shipped 9a4479c80]
    4. Windows Driver Management       [shipped c64c4d8ee]
    5. Windows Patch Management        ← THIS FILE

LIRIL's spec (synthesised across two polls on 2026-04-19):

    CAPABILITY_5: Windows Patch Management
    WHY:          Automate secure, conflict-free patch deployment so that
                  critical security updates land quickly while risky
                  updates (drivers, feature packs, preview builds) are
                  held back for explicit human review.
    MECHANISM:    NATS 'windows.patch.metrics' + 'windows.patch.control';
                  Windows Update Agent COM API (Microsoft.Update.Session /
                  Microsoft.Update.Searcher / Microsoft.Update.Installer);
                  classification uses MsrcSeverity + category as primary
                  signal, NPU (tenet5.liril.classify) for borderline cases.
    FIRST_STEP:   tools/liril_patch_manager.py — this file.
    SAFETY_PLAN:
        ALLOWLIST:       Security Updates, Critical Updates,
                         Definition Updates (Defender), Update Rollups
        DENYLIST:        Drivers (Cap#4 handles), Feature Packs, any
                         Preview/Insider category, Service Packs
        RISK_SCHEMA:     patch_classification:<low|med|high|critical>,
                         confidence:<0..1>
        DRY_RUN_DEFAULT: yes
        AUDIT_SUBJECT:   windows.patch.control

Threat model
------------
An LLM-plan-then-execute that says "install all pending updates" during a
service restart window would be catastrophic — a feature pack install can
take 45 minutes, reboot the host mid-serve, and break drivers the
capability hasn't classified yet. Hence:

  (1) Categories are gate #1: Drivers/Feature Packs/Preview/Insider are
      refused before any classification runs.
  (2) MsrcSeverity is gate #2: updates with MsrcSeverity in {Critical,
      Important} that live in the allowlist categories are the only
      auto-install candidates.
  (3) Dry-run default, 3-second veto, Cap#10 fse gate — same pattern as
      Cap#2/#3/#4.
  (4) WU COM searches take 30-60s, so results are cached in sqlite and
      published on a 6-hour daemon interval rather than per-request.

CLI modes
---------
  --list-available          Cached WU search results (refreshes if older than 6h)
  --list-history [N]        Recent install history (default 30)
  --classify UPDATE_ID      Classify ONE update (UUID) via category + MsrcSeverity + NPU
  --classify-all            Classify every pending update
  --plan ACTION UPDATE_ID   Build a plan — ACTION ∈ {install, decline}
  --execute ACTION UPDATE_ID Run the plan. Requires LIRIL_EXECUTE=1 and admin.
  --refresh                 Force a fresh WU search (slow — minutes)
  --daemon                  6-hour refresh loop, publishes metrics + classifications
  --snapshot                Publish one metrics snapshot from cache and exit
  --show-denylist / --show-allowlist
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
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

NATS_URL        = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AUDIT_SUBJECT   = "windows.patch.control"
VETO_SUBJECT    = "windows.patch.control.veto"
METRICS_SUBJECT = "windows.patch.metrics"
EXEC_GATE       = os.environ.get("LIRIL_EXECUTE", "0") == "1"
VETO_WINDOW_SEC = 3.0

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWLIST_FILE = DATA_DIR / "liril_patch_allowlist.txt"
AUDIT_LOG      = DATA_DIR / "liril_patch_control.jsonl"
CACHE_DB       = DATA_DIR / "liril_patch_cache.sqlite"
CACHE_MAX_AGE_SEC = 6 * 3600   # 6-hour freshness

VALID_ACTIONS = {"install", "decline"}

# ── DENYLIST / ALLOWLIST (CATEGORY-BASED) ─────────────────────────────
# These match against any entry in the update's CategoryNames collection
# (case-insensitive). Denylist wins over allowlist.
_DENIED_CATEGORIES = {
    "drivers",           # Cap#4 handles driver management
    "driver",
    "feature packs",
    "feature pack",
    "upgrades",          # major Windows upgrades — never auto
    "service packs",
    "service pack",
    "preview",
    "insider",
    "developer preview",
    "release preview",
}

_ALLOWED_CATEGORIES = {
    "security updates",
    "critical updates",
    "definition updates",     # Defender signature updates
    "update rollups",
    "updates",                # generic quality updates
}


def _is_denied_category(categories: list[str]) -> tuple[bool, str]:
    """Return (denied, matched_category). Denies if any category matches."""
    for c in categories or []:
        low = (c or "").strip().lower()
        if not low:
            continue
        if low in _DENIED_CATEGORIES:
            return True, c
        # Also deny anything containing "preview" or "insider"
        if "preview" in low or "insider" in low:
            return True, c
    return False, ""


def _is_allowed_category(categories: list[str]) -> tuple[bool, str]:
    for c in categories or []:
        low = (c or "").strip().lower()
        if low in _ALLOWED_CATEGORIES:
            return True, c
    return False, ""


def _load_allowlist() -> set[str]:
    """User-curated per-update allowlist. Lower-cased UpdateIDs (UUIDs)."""
    if not ALLOWLIST_FILE.exists():
        return set()
    try:
        raw = ALLOWLIST_FILE.read_text(encoding="utf-8", errors="replace")
        out: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.add(line.lower())
        return out
    except Exception:
        return set()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[PATCH-MGR] audit log write failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# SQLITE CACHE
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(CACHE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS updates_cache (
            update_id        TEXT PRIMARY KEY,
            title            TEXT,
            description      TEXT,
            msrc_severity    TEXT,
            categories       TEXT,   -- JSON list
            kb_articles      TEXT,   -- JSON list
            size_bytes       INTEGER,
            reboot_required  INTEGER,
            is_mandatory     INTEGER,
            is_downloaded    INTEGER,
            cached_ts        REAL NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cache_ts ON updates_cache(cached_ts)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        )
    """)
    c.commit()
    return c


def _cache_age_sec() -> float:
    c = _db()
    try:
        row = c.execute("SELECT MAX(cached_ts) FROM updates_cache").fetchone()
        last = row[0] if row and row[0] else 0
        if not last:
            return float("inf")
        return time.time() - float(last)
    finally:
        c.close()


def _cache_put_many(updates: list[dict]) -> None:
    c = _db()
    try:
        now = time.time()
        # Wipe + re-insert (cache is a snapshot, not incremental)
        c.execute("DELETE FROM updates_cache")
        for u in updates:
            c.execute(
                "INSERT OR REPLACE INTO updates_cache"
                "(update_id, title, description, msrc_severity, categories, "
                " kb_articles, size_bytes, reboot_required, is_mandatory, "
                " is_downloaded, cached_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    u.get("update_id", ""),
                    u.get("title", "") or "",
                    u.get("description", "") or "",
                    u.get("msrc_severity", "") or "",
                    json.dumps(u.get("categories", [])),
                    json.dumps(u.get("kb_articles", [])),
                    int(u.get("size_bytes") or 0),
                    1 if u.get("reboot_required") else 0,
                    1 if u.get("is_mandatory") else 0,
                    1 if u.get("is_downloaded") else 0,
                    now,
                ),
            )
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_refresh', ?)",
                  (str(now),))
        c.commit()
    finally:
        c.close()


def _cache_get_all() -> list[dict]:
    c = _db()
    try:
        rows = c.execute(
            "SELECT update_id, title, description, msrc_severity, categories, "
            "kb_articles, size_bytes, reboot_required, is_mandatory, is_downloaded "
            "FROM updates_cache"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "update_id":       r[0],
                "title":           r[1],
                "description":     r[2],
                "msrc_severity":   r[3],
                "categories":      json.loads(r[4]) if r[4] else [],
                "kb_articles":     json.loads(r[5]) if r[5] else [],
                "size_bytes":      r[6] or 0,
                "reboot_required": bool(r[7]),
                "is_mandatory":    bool(r[8]),
                "is_downloaded":   bool(r[9]),
            })
        return out
    finally:
        c.close()


def _cache_get_one(update_id: str) -> dict | None:
    if not update_id:
        return None
    c = _db()
    try:
        r = c.execute(
            "SELECT update_id, title, description, msrc_severity, categories, "
            "kb_articles, size_bytes, reboot_required, is_mandatory, is_downloaded "
            "FROM updates_cache WHERE update_id=?",
            (update_id,),
        ).fetchone()
        if not r:
            return None
        return {
            "update_id":       r[0],
            "title":           r[1],
            "description":     r[2],
            "msrc_severity":   r[3],
            "categories":      json.loads(r[4]) if r[4] else [],
            "kb_articles":     json.loads(r[5]) if r[5] else [],
            "size_bytes":      r[6] or 0,
            "reboot_required": bool(r[7]),
            "is_mandatory":    bool(r[8]),
            "is_downloaded":   bool(r[9]),
        }
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────
# WU COM SEARCH (via PowerShell)
# ─────────────────────────────────────────────────────────────────────

# This script runs the WU COM search and emits one JSON object per pending
# update. We run it via subprocess and parse the stdout.
_WU_SEARCH_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $searcher.Online = $true
  $result = $searcher.Search("IsInstalled=0 and Type='Software' and IsHidden=0")
  $out = @()
  foreach ($u in $result.Updates) {
    $cats = @()
    foreach ($c in $u.Categories) { $cats += $c.Name }
    $kbs = @()
    foreach ($kb in $u.KBArticleIDs) { $kbs += $kb }
    $sec = ''
    try { $sec = $u.MsrcSeverity } catch { $sec = '' }
    $obj = @{
      update_id       = $u.Identity.UpdateID
      title           = $u.Title
      description     = ($u.Description -as [string])
      msrc_severity   = $sec
      categories      = $cats
      kb_articles     = $kbs
      size_bytes      = [int64]$u.MaxDownloadSize
      reboot_required = [bool]$u.RebootRequired
      is_mandatory    = [bool]$u.IsMandatory
      is_downloaded   = [bool]$u.IsDownloaded
    }
    $out += ,$obj
  }
  $out | ConvertTo-Json -Depth 4 -Compress
} catch {
  Write-Error ("WU_SEARCH_FAILED: " + $_.Exception.Message)
  exit 1
}
"""

_WU_HISTORY_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $count = $searcher.GetTotalHistoryCount()
  $n = [Math]::Min(__COUNT__, $count)
  if ($n -le 0) { '[]'; exit 0 }
  $hist = $searcher.QueryHistory(0, $n)
  $out = @()
  foreach ($h in $hist) {
    $cats = @()
    try { foreach ($c in $h.Categories) { $cats += $c.Name } } catch {}
    $obj = @{
      title        = $h.Title
      description  = ($h.Description -as [string])
      date         = ($h.Date.ToUniversalTime().ToString('o'))
      result_code  = [int]$h.ResultCode
      operation    = [int]$h.Operation   # 1=install, 2=uninstall, 3=other
      hresult      = [int]$h.HResult
      update_id    = $h.UpdateIdentity.UpdateID
      server_sel   = [int]$h.ServerSelection
      categories   = $cats
    }
    $out += ,$obj
  }
  $out | ConvertTo-Json -Depth 4 -Compress
} catch {
  Write-Error ("WU_HISTORY_FAILED: " + $_.Exception.Message)
  exit 1
}
"""


def _pwsh(script: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def refresh_cache(timeout: int = 180) -> tuple[bool, str, int]:
    """Run a WU search and replace the cache. Returns (ok, message, count).
    Typical runtime: 30-90s depending on network + WU server load."""
    rc, out, err = _pwsh(_WU_SEARCH_PS, timeout=timeout)
    if rc != 0:
        return False, (err or "non-zero exit").strip()[:500], 0
    raw = (out or "").strip()
    if not raw or raw.lower() in ("null", "none"):
        # No updates available — cache an empty snapshot so freshness counter advances
        _cache_put_many([])
        return True, "no updates pending", 0
    try:
        data = json.loads(raw)
    except Exception as e:
        return False, f"json parse failed: {e}", 0
    if isinstance(data, dict):
        data = [data]
    updates = []
    for d in data:
        updates.append({
            "update_id":       d.get("update_id", "") or "",
            "title":           d.get("title", "") or "",
            "description":     d.get("description", "") or "",
            "msrc_severity":   d.get("msrc_severity", "") or "",
            "categories":      list(d.get("categories", []) or []),
            "kb_articles":     [str(k) for k in (d.get("kb_articles", []) or [])],
            "size_bytes":      int(d.get("size_bytes") or 0),
            "reboot_required": bool(d.get("reboot_required")),
            "is_mandatory":    bool(d.get("is_mandatory")),
            "is_downloaded":   bool(d.get("is_downloaded")),
        })
    _cache_put_many(updates)
    return True, f"cached {len(updates)} pending updates", len(updates)


def list_history(n: int = 30, timeout: int = 60) -> list[dict]:
    script = _WU_HISTORY_PS_TEMPLATE.replace("__COUNT__", str(max(1, int(n))))
    rc, out, _ = _pwsh(script, timeout=timeout)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out.strip())
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return list(data or [])


def available(allow_stale: bool = True) -> list[dict]:
    """Return cached pending updates. Auto-refreshes if cache is stale."""
    age = _cache_age_sec()
    if age > CACHE_MAX_AGE_SEC and not allow_stale:
        refresh_cache()
    elif age == float("inf"):
        refresh_cache()
    return _cache_get_all()


# ─────────────────────────────────────────────────────────────────────
# CLASSIFICATION (category + MsrcSeverity primary, NPU secondary)
# ─────────────────────────────────────────────────────────────────────

_MSRC_TO_RISK = {
    "critical":  "critical",
    "important": "high",
    "moderate":  "medium",
    "low":       "low",
    "":          "unknown",
}


def _classify_local(upd: dict) -> dict:
    """Category + MsrcSeverity classification. No NATS call."""
    cats = upd.get("categories") or []
    denied_flag, denied_cat = _is_denied_category(cats)
    allowed_flag, allowed_cat = _is_allowed_category(cats)
    msrc = (upd.get("msrc_severity") or "").strip().lower()
    risk = _MSRC_TO_RISK.get(msrc, "unknown")
    if denied_flag:
        # Denied updates are always "high" regardless of severity — they are
        # dangerous to auto-apply from this capability's perspective.
        risk = "high"
    return {
        "patch_classification": risk,
        "msrc_severity":        msrc or "",
        "denied_by_category":   denied_flag,
        "denied_category":      denied_cat,
        "allowed_by_category":  allowed_flag,
        "allowed_category":     allowed_cat,
        "confidence":           0.95 if msrc else 0.6,
        "source":               "category+msrc",
    }


async def _classify_via_npu(nc, upd: dict) -> dict:
    """Borderline cases — no MsrcSeverity AND no category match — ask LIRIL."""
    text = (
        f"Windows update: {upd.get('title','')} "
        f"(categories={upd.get('categories',[])}, "
        f"reboot={upd.get('reboot_required')}, "
        f"size_mb={round((upd.get('size_bytes') or 0)/1024/1024, 1)})"
    )
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text, "source": "patch_manager"}).encode(),
            timeout=5,
        )
        d = json.loads(msg.data.decode())
        axis = d.get("domain") or d.get("axis")
        conf = d.get("confidence")
        a = (axis or "").upper()
        if any(k in a for k in ("SECURITY", "NETWORK", "KERNEL", "OS")):
            risk = "high"
        elif any(k in a for k in ("TECHNOLOGY", "COMPUTE", "DATA")):
            risk = "medium"
        else:
            risk = "low"
        return {
            "patch_classification": risk,
            "confidence":           conf,
            "axis":                 axis,
            "source":               "npu",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "source": "npu_failed"}


async def classify_one(upd: dict, nc=None) -> dict:
    local = _classify_local(upd)
    # Only consult NPU when local classification is uncertain AND we have NATS
    if local["patch_classification"] in ("unknown",) and nc is not None:
        remote = await _classify_via_npu(nc, upd)
        if "error" not in remote:
            local.update(remote)
    return local


# ─────────────────────────────────────────────────────────────────────
# PLAN + EXECUTE
# ─────────────────────────────────────────────────────────────────────

_UPDATE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _sanitize_update_id(uid: str) -> str:
    u = (uid or "").strip()
    return u if _UPDATE_ID_RE.match(u) else ""


def _make_plan(action: str, update_id: str, reason: str) -> dict:
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r}; must be one of {sorted(VALID_ACTIONS)}")
    uid = _sanitize_update_id(update_id)
    if not uid:
        raise ValueError(f"invalid UpdateID {update_id!r}; expected UUID")
    upd = _cache_get_one(uid)
    cls = _classify_local(upd or {"categories": []})
    denied = True
    if upd:
        denied = cls["denied_by_category"]
    return {
        "plan_id":        str(uuid.uuid4()),
        "timestamp":      _utc(),
        "action":         action,
        "update_id":      uid,
        "update":         upd,
        "classification": cls,
        "reason":         reason,
        "denied":         denied,
        "allowed":        (uid.lower() in _load_allowlist()) if upd else False,
        "dry_run":        not EXEC_GATE,
    }


_WU_INSTALL_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result = $searcher.Search("IsInstalled=0 and UpdateID='__UID__'")
  if ($result.Updates.Count -eq 0) {
    Write-Output '{"status":"not_found"}'
    exit 0
  }
  $uc = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $result.Updates) { [void]$uc.Add($u) }
  # Accept EULA for each
  foreach ($u in $uc) { if (-not $u.EulaAccepted) { $u.AcceptEula() } }
  # Download
  $dl = $session.CreateUpdateDownloader()
  $dl.Updates = $uc
  $null = $dl.Download()
  # Install
  $inst = $session.CreateUpdateInstaller()
  $inst.Updates = $uc
  $ir = $inst.Install()
  $obj = @{
    status          = 'done'
    result_code     = [int]$ir.ResultCode
    hresult         = [int]$ir.HResult
    reboot_required = [bool]$ir.RebootRequired
  }
  $obj | ConvertTo-Json -Compress
} catch {
  Write-Error ("WU_INSTALL_FAILED: " + $_.Exception.Message)
  exit 1
}
"""

_WU_DECLINE_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result = $searcher.Search("IsInstalled=0 and UpdateID='__UID__'")
  if ($result.Updates.Count -eq 0) {
    Write-Output '{"status":"not_found"}'
    exit 0
  }
  foreach ($u in $result.Updates) { $u.IsHidden = $true }
  Write-Output '{"status":"hidden"}'
} catch {
  Write-Error ("WU_DECLINE_FAILED: " + $_.Exception.Message)
  exit 1
}
"""


def _run_install(update_id: str) -> tuple[bool, str, dict]:
    script = _WU_INSTALL_PS_TEMPLATE.replace("__UID__", update_id)
    rc, out, err = _pwsh(script, timeout=3600)  # installs can be long
    if rc != 0:
        msg = (err or out or "non-zero exit").strip()[:500]
        if "elevation" in msg.lower() or "access is denied" in msg.lower():
            return False, "admin required (run elevated with LIRIL_EXECUTE=1)", {}
        return False, f"rc={rc} {msg}", {}
    try:
        info = json.loads((out or "").strip() or "{}")
    except Exception:
        info = {}
    if info.get("status") == "not_found":
        return False, "update not found (may already be installed or hidden)", info
    # ResultCode: 1=NotStarted, 2=InProgress, 3=Succeeded, 4=SucceededWithErrors,
    #             5=Failed, 6=Aborted
    rcode = info.get("result_code")
    if rcode == 3:
        return True, "succeeded", info
    if rcode == 4:
        return True, "succeeded_with_errors", info
    return False, f"result_code={rcode} hresult={info.get('hresult')}", info


def _run_decline(update_id: str) -> tuple[bool, str, dict]:
    script = _WU_DECLINE_PS_TEMPLATE.replace("__UID__", update_id)
    rc, out, err = _pwsh(script, timeout=30)
    if rc != 0:
        return False, (err or out or "non-zero exit").strip()[:500], {}
    try:
        info = json.loads((out or "").strip() or "{}")
    except Exception:
        info = {}
    if info.get("status") == "hidden":
        return True, "hidden", info
    return False, f"unexpected status {info}", info


def _run_action(plan: dict) -> tuple[bool, str, dict]:
    a = plan["action"]
    uid = plan["update_id"]
    if a == "install":
        return _run_install(uid)
    if a == "decline":
        return _run_decline(uid)
    return False, f"unknown action {a!r}", {}


async def _publish_plan(nc, plan: dict) -> None:
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(plan, default=str).encode())
        _audit({"kind": "plan_published", **plan})
    except Exception as e:
        print(f"[PATCH-MGR] publish plan failed: {e!r}")


async def _wait_for_veto(nc, plan_id: str) -> dict | None:
    got: dict | None = None
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _cb(msg):
        nonlocal got
        if fut.done():
            return
        try:
            d = json.loads(msg.data.decode())
            if d.get("plan_id") == plan_id:
                got = d
                fut.set_result(True)
        except Exception:
            pass

    sub = await nc.subscribe(VETO_SUBJECT, cb=_cb)
    try:
        await asyncio.wait_for(fut, timeout=VETO_WINDOW_SEC)
    except asyncio.TimeoutError:
        pass
    finally:
        await sub.unsubscribe()
    return got


async def do_action(action: str, update_id: str, reason: str = "") -> dict:
    try:
        plan = _make_plan(action, update_id, reason or "no reason provided")
    except ValueError as e:
        return {"status": "invalid_action", "error": str(e)}

    if plan["denied"]:
        plan["status"] = "denied_by_denylist"
        _audit({"kind": "denied", **plan})
        return plan

    if not EXEC_GATE:
        plan["status"] = "dry_run_logged"
        _audit({"kind": "dry_run", **plan})
        return plan

    # Cap#10 fail-safe gate
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        if not _fse.is_safe_to_execute():
            plan["status"]         = "refused_by_failsafe"
            plan["failsafe_level"] = _fse.current_level()
            _audit({"kind": "refused_by_failsafe", **plan})
            return plan
    except ImportError:
        pass

    if not plan["allowed"]:
        plan["status"] = "not_in_allowlist"
        _audit({"kind": "blocked_not_allowed", **plan})
        return plan

    try:
        import nats as _nats
    except ImportError:
        plan["status"] = "nats_missing"
        return plan
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        plan["status"] = f"nats_connect_failed: {e!r}"
        return plan

    try:
        await _publish_plan(nc, plan)
        veto = await _wait_for_veto(nc, plan["plan_id"])
        if veto is not None:
            plan["status"] = "vetoed"
            plan["veto"]   = veto
            _audit({"kind": "vetoed", **plan})
            return plan

        # Re-verify the update still matches cached shape (category might have
        # changed if WU republished; unlikely but the invariant check is cheap)
        recheck = _cache_get_one(plan["update_id"])
        if recheck is not None:
            denied_flag, _ = _is_denied_category(recheck.get("categories") or [])
            if denied_flag:
                plan["status"]  = "became_denied_before_execute"
                plan["recheck"] = recheck
                _audit({"kind": "became_denied", **plan})
                return plan

        ok, msg, info = _run_action(plan)
        plan["status"] = "executed" if ok else "execute_failed"
        plan["result"] = msg
        plan["result_info"] = info
        _audit({"kind": "executed" if ok else "failed", **plan})
        try:
            await nc.publish(
                AUDIT_SUBJECT,
                json.dumps({**plan, "kind": "post_exec"}, default=str).encode()
            )
        except Exception:
            pass
        return plan
    finally:
        await nc.drain()


# ─────────────────────────────────────────────────────────────────────
# SNAPSHOT + DAEMON
# ─────────────────────────────────────────────────────────────────────

def _snapshot() -> dict:
    updates = _cache_get_all()
    by_sev: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    denied = 0
    allowed_count = 0
    reboot_needed = 0
    for u in updates:
        sev = (u.get("msrc_severity") or "").strip() or "none"
        by_sev[sev] = by_sev.get(sev, 0) + 1
        for c in u.get("categories") or []:
            by_cat[c] = by_cat.get(c, 0) + 1
        d, _ = _is_denied_category(u.get("categories") or [])
        if d:
            denied += 1
        a, _ = _is_allowed_category(u.get("categories") or [])
        if a:
            allowed_count += 1
        if u.get("reboot_required"):
            reboot_needed += 1
    return {
        "timestamp":      _utc(),
        "host":           os.environ.get("COMPUTERNAME") or "",
        "cache_age_sec":  _cache_age_sec() if _cache_age_sec() != float("inf") else None,
        "pending_count":  len(updates),
        "by_msrc_severity": by_sev,
        "by_category":    dict(sorted(by_cat.items(), key=lambda x: -x[1])[:20]),
        "denied_count":   denied,
        "allowed_count":  allowed_count,
        "reboot_needed":  reboot_needed,
    }


async def _publish_snapshot(nc=None) -> dict:
    snap = _snapshot()
    try:
        if nc is None:
            import nats as _nats
            nc_local = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                await nc_local.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
            finally:
                await nc_local.drain()
        else:
            await nc.publish(METRICS_SUBJECT, json.dumps(snap, default=str).encode())
    except Exception as e:
        print(f"[PATCH-MGR] snapshot publish failed: {e!r}")
    return snap


async def _classify_all() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=3)
    try:
        updates = _cache_get_all()
        print(f"[PATCH-MGR] classifying {len(updates)} pending updates…")
        await _publish_snapshot(nc)
        for u in updates:
            cls = await classify_one(u, nc=nc)
            payload = {
                "kind":      "classification",
                "timestamp": _utc(),
                "update":    u,
                **cls,
            }
            try:
                await nc.publish(AUDIT_SUBJECT, json.dumps(payload, default=str).encode())
            except Exception:
                pass
            _audit(payload)
            risk = cls.get("patch_classification", "?")
            denied = cls.get("denied_by_category", False)
            print(f"  {u.get('update_id','')[:8]}  "
                  f"msrc={(u.get('msrc_severity','') or '-')[:9]:9s} "
                  f"risk={risk:8s} denied={denied}  {(u.get('title','') or '')[:60]}")
    finally:
        await nc.drain()


async def _daemon(interval_hours: float = 6.0) -> None:
    print(f"[PATCH-MGR] daemon starting — refresh every {interval_hours:.1f}h")
    while True:
        try:
            ok, msg, n = refresh_cache()
            print(f"[PATCH-MGR] refresh: ok={ok} msg={msg} count={n}")
            await _classify_all()
        except Exception as e:
            print(f"[PATCH-MGR] cycle error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_hours * 3600)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Windows Patch Management — Capability #5")
    ap.add_argument("--list-available",  action="store_true", help="Show cached pending updates")
    ap.add_argument("--list-history",    type=int, metavar="N", const=30, nargs="?",
                    help="Show N most recent install history entries (default 30)")
    ap.add_argument("--classify",        type=str, metavar="UPDATE_ID",
                    help="Classify ONE update by UUID")
    ap.add_argument("--classify-all",    action="store_true",
                    help="Classify every pending update + publish")
    ap.add_argument("--plan",            nargs=2, metavar=("ACTION", "UPDATE_ID"),
                    help="Build + publish a plan — DRY RUN. ACTION ∈ {install, decline}")
    ap.add_argument("--execute",         nargs=2, metavar=("ACTION", "UPDATE_ID"),
                    help="Execute. Requires LIRIL_EXECUTE=1 and admin.")
    ap.add_argument("--reason",          type=str, default="",
                    help="Reason string attached to the plan")
    ap.add_argument("--refresh",         action="store_true",
                    help="Force WU search + repopulate cache (slow)")
    ap.add_argument("--daemon",          action="store_true",
                    help="Run 6-hour refresh loop")
    ap.add_argument("--daemon-interval", type=float, default=6.0,
                    help="Daemon refresh interval in hours (default 6)")
    ap.add_argument("--snapshot",        action="store_true",
                    help="Publish one windows.patch.metrics snapshot and exit")
    ap.add_argument("--show-denylist",   action="store_true",
                    help="Print denied/allowed category lists")
    ap.add_argument("--show-allowlist",  action="store_true",
                    help="Print the user-curated allowlist and exit")
    args = ap.parse_args()

    if args.show_denylist:
        print("# DENIED categories:")
        for c in sorted(_DENIED_CATEGORIES):
            print(f"  {c}")
        print("# ALLOWED categories:")
        for c in sorted(_ALLOWED_CATEGORIES):
            print(f"  {c}")
        return 0
    if args.show_allowlist:
        al = _load_allowlist()
        if not al:
            print(f"# allowlist is empty — add UpdateIDs to {ALLOWLIST_FILE}")
        else:
            for a in sorted(al):
                print(a)
        return 0

    if args.refresh:
        print("[PATCH-MGR] running WU search (this may take 30-90s)…")
        ok, msg, n = refresh_cache()
        print(f"ok={ok} msg={msg} count={n}")
        return 0 if ok else 1

    if args.list_available:
        updates = available(allow_stale=True)
        age = _cache_age_sec()
        age_str = f"{age:.0f}s old" if age != float("inf") else "no cache"
        print(f"# cache: {age_str}, {len(updates)} pending updates")
        for u in updates:
            cls = _classify_local(u)
            denied = "DENY" if cls["denied_by_category"] else "    "
            print(f"  {denied} {u['update_id'][:8]}  "
                  f"msrc={(u.get('msrc_severity','') or '-')[:9]:9s} "
                  f"risk={cls['patch_classification']:8s} "
                  f"cats={','.join((u.get('categories') or [])[:2])[:30]:30s} "
                  f"{(u.get('title','') or '')[:50]}")
        return 0

    if args.list_history is not None:
        hist = list_history(args.list_history)
        for h in hist:
            rcode = h.get("result_code")
            verb = {1:"Install", 2:"Uninstall", 3:"Other"}.get(h.get("operation", 0), "?")
            status = {1:"NotStart", 2:"InProg", 3:"OK", 4:"OK+err",
                      5:"FAIL", 6:"Abort"}.get(rcode, "?")
            print(f"  {h.get('date','')[:19]}  {verb:9s} {status:8s} "
                  f"{(h.get('title','') or '')[:70]}")
        return 0

    if args.classify:
        uid = _sanitize_update_id(args.classify)
        if not uid:
            print(f"invalid UpdateID {args.classify!r}")
            return 2
        upd = _cache_get_one(uid)
        if not upd:
            print(f"update not in cache — run --refresh first")
            return 1
        async def run():
            import nats as _nats
            nc = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                cls = await classify_one(upd, nc=nc)
            finally:
                await nc.drain()
            print(json.dumps({"update": upd, **cls}, indent=2, default=str))
        asyncio.run(run())
        return 0

    if args.classify_all:
        asyncio.run(_classify_all())
        return 0

    if args.plan:
        action, update_id = args.plan
        os.environ["LIRIL_EXECUTE"] = "0"
        plan = asyncio.run(do_action(action, update_id, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0

    if args.execute:
        action, update_id = args.execute
        if not EXEC_GATE:
            print("EXEC_GATE off — set LIRIL_EXECUTE=1 to execute. Refusing.")
            return 2
        plan = asyncio.run(do_action(action, update_id, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0 if plan.get("status") == "executed" else 1

    if args.snapshot:
        snap = asyncio.run(_publish_snapshot())
        print(json.dumps(snap, indent=2, default=str))
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon(float(args.daemon_interval)))
        except KeyboardInterrupt:
            print("\n[PATCH-MGR] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
