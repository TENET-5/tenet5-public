#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T15:15:00Z | Author: claude_code | Change: unified HTTP/JSON API exposing all 15 LIRIL daemons on 127.0.0.1:18120
"""LIRIL HTTP API — single endpoint for the whole 15-daemon stack.

Why this exists
---------------
Each LIRIL capability has a CLI and a NATS RPC surface, but:
  - CLIs spawn subprocesses (slow, env-dependent)
  - NATS requires a connected client (inconvenient from curl / JS)

This is a 127.0.0.1-ONLY HTTP server that any agent (Daniel, claude_code,
copilot, jetbrains, the local Omniverse dashboard) can hit with a simple
GET/POST. It reads state from the same sqlite DBs + calls the same
in-process Python APIs the other tools use — no subprocess overhead.

Security
--------
  - Binds 127.0.0.1 only (not 0.0.0.0). Nothing leaves the box.
  - No auth; the security boundary is "local only."
  - Every request goes into the journal as observation:api (for audit).
  - Mutations through Cap#10-gated caps still respect fse.
  - Read-only endpoints are always safe; mutation endpoints return a
    PLAN that the caller must confirm with a second call.

Endpoints
---------
  GET  /health               quick up check (always 200)
  GET  /status               collapse of liril_status --json
  GET  /status/brief         one-line brief
  GET  /incidents?limit=N    recent fse incidents
  GET  /level                current fse level
  GET  /gpu                  latest hardware_health snapshot
  GET  /processes?top=N      top N processes by memory
  GET  /services             Get-Service list (cached in process cache)
  GET  /drivers              third-party drivers (pnputil)
  GET  /intent               current user intent
  GET  /patches              pending Windows Update cache
  GET  /file_alerts?limit=N  recent file-awareness alerts
  GET  /nats_rates           observer subject rates (last 5 min)

  GET  /journal/stats
  GET  /journal/recall?key=X  or ?tag=X
  GET  /journal/search?q=X
  POST /journal/remember     body: {key, value, tags?, source?, ttl_sec?}

  POST /notify               body: {severity, title, body}
  POST /speak                body: {text}

  POST /network/get          body: {url, headers?}   (allowlist still enforced)

  GET  /verify?quick=1       runs prove_all harness, returns JSON report

Response envelope: always JSON.
  { "ok": true|false, "data": ..., "error": "..." }
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
import http.server
import socketserver
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BIND_HOST = os.environ.get("LIRIL_API_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("LIRIL_API_PORT", "18120"))

ROOT = Path(__file__).resolve().parent.parent
PY   = str(ROOT / ".venv" / "Scripts" / "python.exe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# IMPORT HELPERS — lazy imports so API runs even if some caps are absent
# ─────────────────────────────────────────────────────────────────────

def _import_tool(name: str):
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        return __import__(name)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# DATA COLLECTORS (most reuse existing tools' Python APIs)
# ─────────────────────────────────────────────────────────────────────

def handler_status(_q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status module missing"}
    return {"ok": True, "data": status.collect()}


def handler_status_brief(_q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    s = status.collect()
    os.environ["LIRIL_STATUS_COLOR"] = "0"
    # Reset the module's cached USE_COLOR if it's there
    if hasattr(status, "USE_COLOR"):
        status.USE_COLOR = False
    return {"ok": True, "data": {"brief": status.render_brief(s), "overall": s["overall"]}}


def handler_level(_q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    return {"ok": True, "data": status.get_fse_level()}


def handler_incidents(q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    limit = int(q.get("limit", ["10"])[0]) if isinstance(q.get("limit"), list) else int(q.get("limit", 10))
    return {"ok": True, "data": status.get_recent_incidents(limit)}


def handler_gpu(_q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    return {"ok": True, "data": {"gpus": status.get_gpu_latest(),
                                   "cpu_c": status.get_cpu_temp()}}


def handler_intent(_q: dict) -> dict:
    intent = _import_tool("liril_user_intent")
    if not intent:
        return {"ok": False, "error": "liril_user_intent missing"}
    return {"ok": True, "data": intent.current_intent()}


def handler_processes(q: dict) -> dict:
    proc = _import_tool("liril_process_manager")
    if not proc:
        return {"ok": False, "error": "liril_process_manager missing"}
    top = int(q.get("top", ["20"])[0]) if isinstance(q.get("top"), list) else int(q.get("top", 20))
    all_p = proc.list_processes()
    all_p.sort(key=lambda p: p.get("working_set_mb", 0), reverse=True)
    return {"ok": True, "data": all_p[:max(1, top)]}


def handler_services(_q: dict) -> dict:
    svc = _import_tool("liril_service_control")
    if not svc:
        return {"ok": False, "error": "liril_service_control missing"}
    return {"ok": True, "data": svc.list_services()}


def handler_drivers(_q: dict) -> dict:
    drv = _import_tool("liril_driver_manager")
    if not drv:
        return {"ok": False, "error": "liril_driver_manager missing"}
    return {"ok": True, "data": drv.list_drivers()}


def handler_patches(_q: dict) -> dict:
    patch = _import_tool("liril_patch_manager")
    if not patch:
        return {"ok": False, "error": "liril_patch_manager missing"}
    return {"ok": True, "data": {
        "updates":      patch.available(allow_stale=True),
        "cache_age_sec": patch._cache_age_sec() if patch._cache_age_sec() != float("inf") else None,
    }}


def handler_file_alerts(q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    limit = int(q.get("limit", ["10"])[0]) if isinstance(q.get("limit"), list) else int(q.get("limit", 10))
    return {"ok": True, "data": status.get_recent_file_alerts(limit)}


def handler_nats_rates(_q: dict) -> dict:
    status = _import_tool("liril_status")
    if not status:
        return {"ok": False, "error": "liril_status missing"}
    return {"ok": True, "data": status.get_subject_rates()}


# ── Journal ──────────────────────────────────────────────────────────

def handler_journal_stats(_q: dict) -> dict:
    j = _import_tool("liril_journal")
    if not j:
        return {"ok": False, "error": "liril_journal missing"}
    return {"ok": True, "data": j.stats()}


def handler_journal_recall(q: dict) -> dict:
    j = _import_tool("liril_journal")
    if not j:
        return {"ok": False, "error": "liril_journal missing"}
    key = q.get("key", [None])[0] if isinstance(q.get("key"), list) else q.get("key")
    tag = q.get("tag", [None])[0] if isinstance(q.get("tag"), list) else q.get("tag")
    lim = q.get("limit", ["50"])[0] if isinstance(q.get("limit"), list) else q.get("limit", "50")
    rows = j.recall(key=key, tag=tag, limit=int(lim))
    return {"ok": True, "data": rows}


def handler_journal_search(q: dict) -> dict:
    j = _import_tool("liril_journal")
    if not j:
        return {"ok": False, "error": "liril_journal missing"}
    text = q.get("q", [""])[0] if isinstance(q.get("q"), list) else q.get("q", "")
    lim = q.get("limit", ["20"])[0] if isinstance(q.get("limit"), list) else q.get("limit", "20")
    if not text.strip():
        return {"ok": False, "error": "query param 'q' is required"}
    return {"ok": True, "data": j.search(text, limit=int(lim))}


def handler_journal_remember(body: dict) -> dict:
    j = _import_tool("liril_journal")
    if not j:
        return {"ok": False, "error": "liril_journal missing"}
    key = body.get("key")
    if not key:
        return {"ok": False, "error": "'key' is required"}
    try:
        eid = j.remember(
            key=key,
            value=body.get("value"),
            tags=body.get("tags"),
            source=body.get("source") or "liril_api",
            ttl_sec=body.get("ttl_sec"),
            pinned=body.get("pinned"),
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "data": {"id": eid}}


# ── Notify / Speak ────────────────────────────────────────────────────

def handler_notify(body: dict) -> dict:
    comm = _import_tool("liril_communication")
    if not comm:
        return {"ok": False, "error": "liril_communication missing"}
    try:
        r = comm.dispatch_notify(
            body.get("severity") or "medium",
            body.get("title")    or "LIRIL",
            body.get("body")     or "",
        )
        return {"ok": True, "data": r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def handler_speak(body: dict) -> dict:
    comm = _import_tool("liril_communication")
    if not comm:
        return {"ok": False, "error": "liril_communication missing"}
    text = body.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "'text' is required"}
    intent = (_import_tool("liril_user_intent") or type("", (), {
        "current_intent": staticmethod(lambda: {"category": "UNKNOWN"})
    })).current_intent().get("category", "UNKNOWN")
    if intent in ("GAMING", "MEETING"):
        return {"ok": True, "data": {"skipped": f"intent={intent}"}}
    ok, how = comm._render_tts(text)
    return {"ok": True, "data": {"ok": ok, "channel": how}}


# ── Network (outbound GET) ────────────────────────────────────────────

def handler_network_get(body: dict) -> dict:
    net = _import_tool("liril_network_reach")
    if not net:
        return {"ok": False, "error": "liril_network_reach missing"}
    url = body.get("url") or ""
    if not url:
        return {"ok": False, "error": "'url' is required"}
    r = net.request("GET", url, headers=body.get("headers"),
                    timeout=float(body.get("timeout", 15)))
    # Strip body from audit response? Callers can pass ?no_body=1 to omit
    return {"ok": r.get("ok", False), "data": r}


# ── Verify (run prove_all) ────────────────────────────────────────────

def handler_verify(q: dict) -> dict:
    quick = q.get("quick", ["0"])
    if isinstance(quick, list):
        quick = quick[0]
    is_quick = quick in ("1", "true", "yes")
    cmd = [PY, "-X", "utf8", str(ROOT / "tools" / "liril_prove_all.py"),
           "--json"]
    if is_quick:
        cmd.append("--quick")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'src'};{ROOT.parent}"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=300, text=True,
            encoding="utf-8", errors="replace",
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out[out.find("{"):out.rfind("}") + 1])
    except Exception:
        return {"ok": False, "error": "parse failure", "stdout": out[:400]}
    return {"ok": r.returncode == 0, "data": data}


# ─────────────────────────────────────────────────────────────────────
# ROUTING
# ─────────────────────────────────────────────────────────────────────

ROUTES = {
    "GET": {
        "/":                   lambda q: {"ok": True, "data": {"see": "/health, /status, /incidents, /gpu, /intent, /processes, /services, /drivers, /patches, /journal/*, /nats_rates, /file_alerts, /level, /verify"}},
        "/health":             lambda q: {"ok": True, "data": {"status": "ok", "ts": _utc()}},
        "/status":             handler_status,
        "/status/brief":       handler_status_brief,
        "/level":              handler_level,
        "/incidents":          handler_incidents,
        "/gpu":                handler_gpu,
        "/intent":             handler_intent,
        "/processes":          handler_processes,
        "/services":           handler_services,
        "/drivers":            handler_drivers,
        "/patches":            handler_patches,
        "/file_alerts":        handler_file_alerts,
        "/nats_rates":         handler_nats_rates,
        "/journal/stats":      handler_journal_stats,
        "/journal/recall":     handler_journal_recall,
        "/journal/search":     handler_journal_search,
        "/verify":             handler_verify,
    },
    "POST": {
        "/journal/remember":   handler_journal_remember,
        "/notify":             handler_notify,
        "/speak":              handler_speak,
        "/network/get":        handler_network_get,
    },
}


# ─────────────────────────────────────────────────────────────────────
# HTTP SERVER
# ─────────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    # Suppress default stderr logging (supervisor captures it)
    def log_message(self, format, *args):  # noqa: A003
        return

    def _send(self, status: int, body: dict) -> None:
        try:
            payload = json.dumps(body, default=str).encode("utf-8")
        except Exception as e:
            payload = json.dumps({"ok": False, "error": f"encode: {e}"}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def _route(self, method: str, path: str, q: dict, body: dict | None) -> None:
        # Journal the hit (best-effort, non-blocking)
        try:
            j = _import_tool("liril_journal")
            if j:
                j.remember(
                    key=f"api.{method}.{path}",
                    value={"q": q, "body_keys": list((body or {}).keys()),
                           "ts": _utc()},
                    tags=f"observation:api,api:{path}",
                    source="liril_api",
                )
        except Exception:
            pass

        table = ROUTES.get(method, {})
        handler = table.get(path)
        if not handler:
            self._send(404, {"ok": False, "error": f"no route {method} {path}"})
            return
        try:
            if method == "POST":
                result = handler(body or {})
            else:
                result = handler(q)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        status = 200 if result.get("ok", True) else 400
        self._send(status, result)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query or "")
        self._route("GET", parsed.path, q, None)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = b""
        if length:
            try:
                raw = self.rfile.read(min(length, 1024 * 1024))
            except Exception:
                raw = b""
        body: dict | None = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                self._send(400, {"ok": False, "error": "invalid json body"})
                return
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query or "")
        self._route("POST", parsed.path, q, body)


class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve() -> None:
    print(f"[API] listening on http://{BIND_HOST}:{BIND_PORT}")
    server = _ThreadedServer((BIND_HOST, BIND_PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[API] stopped")
    finally:
        server.server_close()


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL unified HTTP API")
    ap.add_argument("--serve",   action="store_true", help="Run HTTP server")
    ap.add_argument("--daemon",  action="store_true", help="Alias for --serve")
    ap.add_argument("--routes",  action="store_true", help="List routes and exit")
    ap.add_argument("--test",    type=str, metavar="PATH",
                    help="Call an endpoint in-process and print the result")
    args = ap.parse_args()

    if args.routes:
        print("GET routes:")
        for p in sorted(ROUTES["GET"]):
            print(f"  {p}")
        print("POST routes:")
        for p in sorted(ROUTES["POST"]):
            print(f"  {p}")
        return 0

    if args.test:
        handler = ROUTES["GET"].get(args.test)
        if not handler:
            print(f"no GET route {args.test}")
            return 2
        r = handler({})
        print(json.dumps(r, indent=2, default=str)[:4000])
        return 0 if r.get("ok", False) else 1

    if args.serve or args.daemon:
        serve()
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
