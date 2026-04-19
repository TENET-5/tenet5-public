#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T10:15:00Z | Author: claude_code | Change: wire fse.is_safe_to_execute() gate (Cap#10 enforcement)
"""LIRIL Windows Driver Management — Capability #4 of the NPU-Domain plan.

LIRIL's ordering (from the NPU-Domain poll that seeded #1–#5):
    1. Windows System Monitoring       [shipped tools/liril_windows_monitor.py]
    2. Windows Service Control         [shipped tools/liril_service_control.py]
    3. Windows Process Management      [shipped tools/liril_process_manager.py]
    4. Windows Driver Management       ← THIS FILE
    5. Windows Patch Management

Spec for #4 (per LIRIL's poll on 2026-04-19 when asked "what capabilities do you
need next?"):

    CAPABILITY_4: Windows Driver Management
    WHY:          Safe, auditable driver install/remove/enable/disable to
                  prevent system instability.
    MECHANISM:    NATS subjects 'windows.driver.metrics' + 'windows.driver.control';
                  Windows pnputil.exe + Get-PnpDevice / Disable-PnpDevice /
                  Enable-PnpDevice; NPU criticality classification via
                  tenet5.liril.classify.
    FIRST_STEP:   tools/liril_driver_manager.py — this file.
    SAFETY_PLAN:
        ALLOWLIST:       empty (user-curated INF names, case-insensitive)
        DENYLIST:        class-based (Display/Net/System/USB/SCSIAdapter/HDC/
                         Keyboard/Mouse/HIDClass) + catalog-specific overrides
                         (nvlddmkm, iigdkmd64, stornvme, ntfs, tcpip, ndis,
                         acpi, usbhub, fastfat, and Intel NPU drivers).
        RISK_SCHEMA:     driver_classification:<low|med|high|critical>,
                         confidence:<0..1>
        DRY_RUN_DEFAULT: yes
        AUDIT_SUBJECT:   windows.driver.control

Threat model
------------
Same as Cap#2/#3: a confidently-wrong LLM plan like "remove oem8.inf (nv_dispi)
because GPU0 showed high memory" would un-install NVIDIA display drivers and
brick the compute surface. Therefore:
  (1) Every mutation publishes to windows.driver.control BEFORE execution.
  (2) Default is dry-run.
  (3) Execution requires --execute AND env LIRIL_EXECUTE=1 AND class-based
      denylist gate AND INF still present after veto window.
  (4) 3-second veto window on windows.driver.control.veto with plan_id.
  (5) Boot-critical driver classes are hard-coded and non-overridable via NATS.
  (6) pnputil itself requires admin for mutations; a non-admin run will fail
      at the subprocess layer with a descriptive error rather than pretending
      the action succeeded.

CLI modes
---------
  --list                   List all third-party drivers (pnputil /enum-drivers)
  --devices                List all PnP devices (Get-PnpDevice) with class + status
  --classify INF_OR_DEVICE Classify a driver OR device (auto-detected) via NPU
  --classify-all           Classify every driver — publish each
  --plan ACTION TARGET     Build a plan + publish — DRY RUN.
                           ACTION ∈ {uninstall, disable, enable, restart}
                             uninstall TARGET=oemN.inf (published name)
                             disable/enable/restart TARGET=instance-id (PnP)
  --execute ACTION TARGET  Execute. Requires LIRIL_EXECUTE=1 env.
  --daemon                 Classify-all every N minutes, publish metrics
  --snapshot               One-shot publish windows.driver.metrics and exit
  --show-denylist / --show-allowlist

Parallels to Cap#2/#3
---------------------
  Same plan→publish→veto→re-check→execute pipeline. Differences:
    * Target type is polymorphic (INF name for uninstall, PnP instance-id for
      device-level actions). The plan schema carries a `target_type` field.
    * Re-check step verifies the INF is still registered (uninstall) or the
      device still exists with the same InstanceId (disable/enable/restart).
    * Class Name matters more than specific INF identity — the denylist is
      class-primary. If Class ∈ {Display, Net, System, ...} we refuse the plan
      before even building it.
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
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

NATS_URL        = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AUDIT_SUBJECT   = "windows.driver.control"
VETO_SUBJECT    = "windows.driver.control.veto"
METRICS_SUBJECT = "windows.driver.metrics"
EXEC_GATE       = os.environ.get("LIRIL_EXECUTE", "0") == "1"
VETO_WINDOW_SEC = 3.0

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWLIST_FILE = DATA_DIR / "liril_driver_allowlist.txt"
AUDIT_LOG      = DATA_DIR / "liril_driver_control.jsonl"

VALID_ACTIONS = {"uninstall", "disable", "enable", "restart"}

# ── DENYLIST (CLASS-BASED) ────────────────────────────────────────────
# Any driver whose Class Name matches these is refused before the plan is
# even built. These are boot-critical categories whose removal can prevent
# Windows from booting or cripple basic I/O.
_DENIED_CLASSES = {
    "Display",          # GPU drivers (nvlddmkm, iigdkmd64, amdkmpfd)
    "Net",              # NICs, WiFi, virtual adapters
    "System",           # chipset, ACPI, platform drivers
    "USB",              # root hubs, controllers
    "SCSIAdapter",      # storage controllers
    "HDC",              # hard-disk controllers
    "Keyboard",         # input
    "Mouse",
    "HIDClass",
    "DiskDrive",        # storage devices
    "Volume",           # volume manager
    "Battery",          # power
    "ACPI",
    "SecurityDevices",  # TPM, Windows Hello
    "Biometric",
    "Processor",        # CPU-level drivers
    "Extension",        # firmware extensions (touchy — better to refuse)
    "Firmware",
    "Computer",         # machine-level
}

# Catalog-specific overrides — exact OR prefix match on Original Name
# (lowercased, with or without .inf). Even if the class weren't critical,
# these specific drivers must never be removed because TENET5 depends on them.
_DENIED_ORIGINAL_PREFIXES = (
    "nvlddmkm", "nv_dispi", "nv_", "nvgpc",          # NVIDIA display
    "iigdkmd", "iigd_dch", "iigd_",                  # Intel integrated GPU
    "amdkmpfd", "amdkmdag", "amd_",                  # AMD display
    "tcpip",                                         # TCP/IP stack
    "ndis",                                          # network driver interface
    "ntfs", "fastfat", "exfat", "refs",              # filesystem drivers
    "stornvme", "iastora", "iastorac", "storahci",   # storage
    "usbhub", "usbport", "usbxhci", "usbccgp",       # USB
    "acpi",                                          # power/platform
    "intelaudio", "intelaiix",                       # Intel NPU/audio
    "wintun", "wireguard",                           # secure tunnels
    "windefend", "mpssvc",                           # Defender/firewall kernel
)


def _is_denied_by_class(class_name: str) -> bool:
    if not class_name:
        return True  # unknown class → refuse (fail-safe)
    return class_name.strip() in _DENIED_CLASSES


def _is_denied_by_original(original_name: str) -> bool:
    if not original_name:
        return False
    low = original_name.strip().lower()
    if low.endswith(".inf"):
        low = low[:-4]
    for p in _DENIED_ORIGINAL_PREFIXES:
        if low == p or low.startswith(p):
            return True
    return False


def _is_denied(driver: dict) -> bool:
    if _is_denied_by_class(driver.get("class_name", "") or ""):
        return True
    if _is_denied_by_original(driver.get("original_name", "") or ""):
        return True
    return False


def _load_allowlist() -> set[str]:
    """User-curated allowlist. Lower-cased INF published names (oem*.inf)."""
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
        print(f"[DRV-MGR] audit log write failed: {e!r}")


# ─────────────────────────────────────────────────────────────────────
# DRIVER + DEVICE DISCOVERY
# ─────────────────────────────────────────────────────────────────────

def _run_capture(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


_DRV_FIELD_RE = re.compile(r"^\s*([A-Za-z ]+?)\s*:\s+(.*)$")

def list_drivers() -> list[dict]:
    """Parse `pnputil /enum-drivers` block output into structured dicts."""
    rc, out, err = _run_capture(["pnputil.exe", "/enum-drivers"], timeout=40)
    if rc != 0 or not out:
        return []
    drivers: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                drivers.append(cur)
                cur = {}
            continue
        m = _DRV_FIELD_RE.match(line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        key_l = key.lower().replace(" ", "_")
        cur[key_l] = val
    if cur:
        drivers.append(cur)
    # Normalise keys: published_name, original_name, provider_name, class_name,
    # class_guid, driver_version, signer_name
    out_list = []
    for d in drivers:
        out_list.append({
            "published_name":  d.get("published_name", ""),
            "original_name":   d.get("original_name", ""),
            "provider_name":   d.get("provider_name", ""),
            "class_name":      d.get("class_name", ""),
            "class_guid":      d.get("class_guid", ""),
            "driver_version":  d.get("driver_version", ""),
            "signer_name":     d.get("signer_name", ""),
            "extension_id":    d.get("extension_id", ""),
        })
    return out_list


def driver_by_published(name: str) -> dict | None:
    safe = _sanitize_inf(name)
    if not safe:
        return None
    for d in list_drivers():
        if d.get("published_name", "").lower() == safe.lower():
            return d
    return None


def _sanitize_inf(name: str) -> str:
    """Accept only oemN.inf style names (digits + .inf) for mutations."""
    m = re.match(r"^\s*(oem\d+\.inf)\s*$", (name or "").strip().lower())
    return m.group(1) if m else ""


def _sanitize_instance_id(iid: str) -> str:
    """Allow the characters PnP instance IDs actually use. No shell metacharacters."""
    return re.sub(r"[^A-Za-z0-9_\-\\{}.&]", "", iid or "")


def list_devices(class_filter: str | None = None) -> list[dict]:
    """Use Get-PnpDevice to enumerate devices. No admin required."""
    if class_filter:
        clf = re.sub(r"[^A-Za-z0-9]", "", class_filter)
        filt = f" -Class '{clf}'"
    else:
        filt = ""
    script = (
        f"Get-PnpDevice -PresentOnly{filt} | "
        "Select-Object FriendlyName,InstanceId,Class,Status,"
        "@{n='Problem';e={$_.Problem}} | ConvertTo-Json -Compress"
    )
    rc, out, _ = _run_capture(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=30,
    )
    if rc != 0 or not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out_list = []
    for d in data:
        out_list.append({
            "friendly_name": d.get("FriendlyName", "") or "",
            "instance_id":   d.get("InstanceId", "") or "",
            "class":         d.get("Class", "") or "",
            "status":        d.get("Status", "") or "",
            "problem":       d.get("Problem", 0),
        })
    return out_list


def device_state(instance_id: str) -> dict | None:
    safe = _sanitize_instance_id(instance_id)
    if not safe:
        return None
    script = (
        f"Get-PnpDevice -InstanceId '{safe}' -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,InstanceId,Class,Status,"
        "@{n='Problem';e={$_.Problem}} | ConvertTo-Json -Compress"
    )
    rc, out, _ = _run_capture(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=10,
    )
    if rc != 0 or not out:
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    if isinstance(d, list):
        d = d[0] if d else None
    if not isinstance(d, dict):
        return None
    return {
        "friendly_name": d.get("FriendlyName", "") or "",
        "instance_id":   d.get("InstanceId", "") or "",
        "class":         d.get("Class", "") or "",
        "status":        d.get("Status", "") or "",
        "problem":       d.get("Problem", 0),
    }


# ─────────────────────────────────────────────────────────────────────
# NPU CLASSIFY
# ─────────────────────────────────────────────────────────────────────

async def _classify_via_npu(nc, driver: dict) -> dict:
    text = (
        f"Windows driver: {driver.get('original_name','')} "
        f"(published={driver.get('published_name','')}, "
        f"class={driver.get('class_name','')}, "
        f"provider={driver.get('provider_name','')}, "
        f"signer={driver.get('signer_name','')}, "
        f"version={driver.get('driver_version','')})"
    )
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text, "source": "driver_manager"}).encode(),
            timeout=5,
        )
        d = json.loads(msg.data.decode())
        axis = d.get("domain") or d.get("axis")
        conf = d.get("confidence")
        return {
            "driver_classification": _axis_to_risk(axis, driver),
            "confidence": conf,
            "axis": axis,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _axis_to_risk(axis: str | None, driver: dict) -> str:
    """Class-based denial forces 'critical' regardless of LIRIL's axis."""
    if _is_denied(driver):
        return "critical"
    if not axis:
        return "unknown"
    a = axis.upper()
    if any(k in a for k in ("SECURITY", "NETWORK", "KERNEL", "OS")):
        return "critical"
    if any(k in a for k in ("ETHICS", "SURVEILLANCE", "IDENTITY")):
        return "high"
    if any(k in a for k in ("TECHNOLOGY", "COMPUTE", "DATA")):
        return "medium"
    return "low"


# ─────────────────────────────────────────────────────────────────────
# PLAN + EXECUTE
# ─────────────────────────────────────────────────────────────────────

def _make_plan(action: str, target: str, reason: str) -> dict:
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r}; must be one of {sorted(VALID_ACTIONS)}")

    if action == "uninstall":
        pub = _sanitize_inf(target)
        if not pub:
            raise ValueError(f"invalid INF name {target!r}; uninstall requires oemN.inf")
        drv = driver_by_published(pub)
        denied = _is_denied(drv) if drv else True  # unknown INF → deny
        allowed = pub.lower() in _load_allowlist()
        return {
            "plan_id":     str(uuid.uuid4()),
            "timestamp":   _utc(),
            "action":      "uninstall",
            "target_type": "inf",
            "target":      pub,
            "driver":      drv,
            "reason":      reason,
            "denied":      denied,
            "allowed":     allowed,
            "dry_run":     not EXEC_GATE,
        }

    # disable / enable / restart — target is a PnP instance id
    safe_iid = _sanitize_instance_id(target)
    if not safe_iid:
        raise ValueError(f"invalid instance id {target!r}")
    dev = device_state(safe_iid)
    denied = True
    if dev:
        denied = _is_denied_by_class(dev.get("class", "") or "")
    return {
        "plan_id":     str(uuid.uuid4()),
        "timestamp":   _utc(),
        "action":      action,
        "target_type": "instance_id",
        "target":      safe_iid,
        "device":      dev,
        "reason":      reason,
        "denied":      denied,
        "allowed":     safe_iid.lower() in _load_allowlist(),
        "dry_run":     not EXEC_GATE,
    }


async def _publish_plan(nc, plan: dict) -> None:
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(plan).encode())
        _audit({"kind": "plan_published", **plan})
    except Exception as e:
        print(f"[DRV-MGR] publish plan failed: {e!r}")


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


def _run_uninstall(published_inf: str) -> tuple[bool, str]:
    rc, out, err = _run_capture(
        ["pnputil.exe", "/delete-driver", published_inf, "/uninstall"],
        timeout=60,
    )
    msg = (err or out or "").strip()[:500]
    if rc == 0:
        return True, msg or "ok"
    if "elevation" in msg.lower() or "access is denied" in msg.lower() or rc == 5:
        return False, "admin required (run elevated with LIRIL_EXECUTE=1)"
    return False, f"rc={rc} {msg}"


def _run_pnp_device_action(action: str, instance_id: str) -> tuple[bool, str]:
    pnp_flag = {
        "disable": "/disable-device",
        "enable":  "/enable-device",
        "restart": "/restart-device",
    }.get(action)
    if not pnp_flag:
        return False, f"unknown action {action!r}"
    rc, out, err = _run_capture(
        ["pnputil.exe", pnp_flag, instance_id],
        timeout=60,
    )
    msg = (err or out or "").strip()[:500]
    if rc == 0:
        return True, msg or "ok"
    if "elevation" in msg.lower() or "access is denied" in msg.lower() or rc == 5:
        return False, "admin required (run elevated with LIRIL_EXECUTE=1)"
    return False, f"rc={rc} {msg}"


def _run_action(plan: dict) -> tuple[bool, str]:
    a = plan["action"]
    if a == "uninstall":
        return _run_uninstall(plan["target"])
    if a in ("disable", "enable", "restart"):
        return _run_pnp_device_action(a, plan["target"])
    return False, f"unknown action {a!r}"


async def do_action(action: str, target: str, reason: str = "") -> dict:
    try:
        plan = _make_plan(action, target, reason or "no reason provided")
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

    # Cap#10 fail-safe gate — refuse if the global escalation level restricts
    # mutations. Missing module is non-fatal.
    try:
        import liril_fail_safe_escalation as _fse
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

        # Re-verify the target still exists and hasn't changed shape
        if plan["target_type"] == "inf":
            recheck = driver_by_published(plan["target"])
            if recheck is None:
                plan["status"] = "inf_gone_before_execute"
                _audit({"kind": "inf_gone", **plan})
                return plan
            if _is_denied(recheck):
                plan["status"]  = "became_denied_before_execute"
                plan["recheck"] = recheck
                _audit({"kind": "became_denied", **plan})
                return plan
        else:
            recheck = device_state(plan["target"])
            if recheck is None:
                plan["status"] = "device_gone_before_execute"
                _audit({"kind": "device_gone", **plan})
                return plan
            if _is_denied_by_class(recheck.get("class", "") or ""):
                plan["status"]  = "became_denied_before_execute"
                plan["recheck"] = recheck
                _audit({"kind": "became_denied", **plan})
                return plan

        ok, msg = _run_action(plan)
        plan["status"] = "executed" if ok else "execute_failed"
        plan["result"] = msg
        _audit({"kind": "executed" if ok else "failed", **plan})

        if plan["target_type"] == "inf":
            plan["driver_state_after"] = driver_by_published(plan["target"])
        else:
            plan["device_state_after"] = device_state(plan["target"])
        try:
            await nc.publish(
                AUDIT_SUBJECT,
                json.dumps({**plan, "kind": "post_exec"}).encode()
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
    drivers = list_drivers()
    devices = list_devices()
    by_class: dict[str, int] = {}
    for d in drivers:
        cls = d.get("class_name") or "<none>"
        by_class[cls] = by_class.get(cls, 0) + 1
    problem_devs = [d for d in devices if (d.get("problem") or 0) != 0]
    return {
        "timestamp":     _utc(),
        "host":          os.environ.get("COMPUTERNAME") or "",
        "driver_count":  len(drivers),
        "device_count":  len(devices),
        "by_class":      dict(sorted(by_class.items(), key=lambda x: -x[1])),
        "problem_count": len(problem_devs),
        "problem_devices": problem_devs[:20],
    }


async def _publish_snapshot(nc=None) -> dict:
    snap = _snapshot()
    try:
        if nc is None:
            import nats as _nats
            nc_local = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                await nc_local.publish(METRICS_SUBJECT, json.dumps(snap).encode())
            finally:
                await nc_local.drain()
        else:
            await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
    except Exception as e:
        print(f"[DRV-MGR] snapshot publish failed: {e!r}")
    return snap


async def _classify_all() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=3)
    try:
        drivers = list_drivers()
        print(f"[DRV-MGR] classifying {len(drivers)} drivers via tenet5.liril.classify…")
        await _publish_snapshot(nc)
        for d in drivers:
            cls = await _classify_via_npu(nc, d)
            payload = {
                "kind":      "classification",
                "timestamp": _utc(),
                "driver":    d,
                **cls,
                "denied":    _is_denied(d),
            }
            try:
                await nc.publish(AUDIT_SUBJECT, json.dumps(payload).encode())
            except Exception:
                pass
            _audit(payload)
            risk = cls.get("driver_classification", "?")
            print(f"  {d.get('published_name','?'):14s} "
                  f"{(d.get('original_name','') or '')[:28]:28s} "
                  f"class={(d.get('class_name','') or '')[:16]:16s} "
                  f"risk={risk}  denied={_is_denied(d)}")
    finally:
        await nc.drain()


async def _daemon(interval_min: int = 30) -> None:
    print(f"[DRV-MGR] daemon started — classify-all every {interval_min} min")
    while True:
        try:
            await _classify_all()
        except Exception as e:
            print(f"[DRV-MGR] cycle error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_min * 60)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Windows Driver Management — Capability #4")
    ap.add_argument("--list",            action="store_true", help="List third-party drivers")
    ap.add_argument("--devices",         action="store_true", help="List PnP devices")
    ap.add_argument("--devices-class",   type=str, default=None,
                    help="Filter devices by class (e.g. 'Display', 'Net')")
    ap.add_argument("--classify",        type=str, metavar="INF_OR_INSTANCE",
                    help="Classify ONE driver (oemN.inf) or device (InstanceId)")
    ap.add_argument("--classify-all",    action="store_true",
                    help="Classify every driver + publish")
    ap.add_argument("--plan",            nargs=2, metavar=("ACTION", "TARGET"),
                    help="Build + publish a plan — DRY RUN. "
                         "ACTION ∈ {uninstall,disable,enable,restart}. "
                         "TARGET = oemN.inf for uninstall, InstanceId otherwise.")
    ap.add_argument("--execute",         nargs=2, metavar=("ACTION", "TARGET"),
                    help="Execute. Requires LIRIL_EXECUTE=1 and admin.")
    ap.add_argument("--reason",          type=str, default="",
                    help="Reason string attached to the plan")
    ap.add_argument("--daemon",          action="store_true",
                    help="Run classify-all daemon (30-min loop)")
    ap.add_argument("--daemon-interval", type=int, default=30,
                    help="Daemon interval in minutes (default 30)")
    ap.add_argument("--snapshot",        action="store_true",
                    help="Publish one windows.driver.metrics snapshot and exit")
    ap.add_argument("--show-denylist",   action="store_true",
                    help="Print denied classes and denied original-name prefixes")
    ap.add_argument("--show-allowlist",  action="store_true",
                    help="Print the user-curated allowlist and exit")
    args = ap.parse_args()

    if args.show_denylist:
        print("# Denied CLASSES:")
        for c in sorted(_DENIED_CLASSES):
            print(f"  class: {c}")
        print("# Denied ORIGINAL NAME prefixes:")
        for p in sorted(_DENIED_ORIGINAL_PREFIXES):
            print(f"  prefix: {p}")
        return 0
    if args.show_allowlist:
        al = _load_allowlist()
        if not al:
            print(f"# allowlist is empty — add INF names to {ALLOWLIST_FILE}")
        else:
            for a in sorted(al):
                print(a)
        return 0

    if args.list:
        drivers = list_drivers()
        drivers.sort(key=lambda d: (d.get("class_name", ""), d.get("published_name", "")))
        for d in drivers:
            flag = "DENY" if _is_denied(d) else "    "
            print(f"  {flag} {d.get('published_name',''):14s} "
                  f"{(d.get('original_name','') or '')[:32]:32s} "
                  f"class={(d.get('class_name','') or '')[:18]:18s} "
                  f"provider={(d.get('provider_name','') or '')[:28]:28s}")
        return 0

    if args.devices:
        devs = list_devices(args.devices_class)
        devs.sort(key=lambda d: (d.get("class", ""), d.get("friendly_name", "")))
        for d in devs:
            print(f"  class={(d.get('class','') or '')[:16]:16s} "
                  f"status={(d.get('status','') or '')[:8]:8s} "
                  f"problem={d.get('problem',0):>3}  "
                  f"{(d.get('friendly_name','') or '')[:60]}")
        return 0

    if args.classify:
        async def run():
            import nats as _nats
            nc = await _nats.connect(NATS_URL, connect_timeout=3)
            try:
                target = args.classify.strip()
                if _sanitize_inf(target):
                    d = driver_by_published(target)
                    if not d:
                        print(f"INF {target!r} not installed")
                        return
                    cls = await _classify_via_npu(nc, d)
                    print(json.dumps({"driver": d, **cls, "denied": _is_denied(d)}, indent=2))
                else:
                    dev = device_state(target)
                    if not dev:
                        print(f"device instance {target!r} not found")
                        return
                    # Treat as device — risk from class alone
                    denied = _is_denied_by_class(dev.get("class", "") or "")
                    print(json.dumps({"device": dev, "denied": denied}, indent=2))
            finally:
                await nc.drain()
        asyncio.run(run())
        return 0

    if args.classify_all:
        asyncio.run(_classify_all())
        return 0

    if args.plan:
        action, target = args.plan
        os.environ["LIRIL_EXECUTE"] = "0"
        plan = asyncio.run(do_action(action, target, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0

    if args.execute:
        action, target = args.execute
        if not EXEC_GATE:
            print("EXEC_GATE off — set LIRIL_EXECUTE=1 to execute. Refusing.")
            return 2
        plan = asyncio.run(do_action(action, target, args.reason))
        print(json.dumps(plan, indent=2, default=str))
        return 0 if plan.get("status") == "executed" else 1

    if args.snapshot:
        snap = asyncio.run(_publish_snapshot())
        print(json.dumps(snap, indent=2))
        return 0

    if args.daemon:
        asyncio.run(_daemon(int(args.daemon_interval)))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
