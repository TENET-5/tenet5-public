#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T12:45:00Z | Author: claude_code | Change: LIRIL Skill — Outbound Network Reach (allowlist, rate limit, fse gate)
"""LIRIL Outbound Network Reach — LIRIL's first gateway-to-the-world skill.

LIRIL picked this at severity=CRITICAL when asked what new skills she
wanted: "Allows critical API integrations and remote resource coordination."

Why this is a separate capability
---------------------------------
Every capability so far (Cap#1–#10) operates on the local host. The
moment LIRIL reaches out to an external service, the threat model
changes:
  - DNS can be tampered with
  - TLS errors must be treated as incidents, not retries
  - Auth tokens must not leak to the audit log
  - A compromised LIRIL could be used to exfiltrate data
  - A faulty retry loop could DDoS a partner API

Therefore this skill is the SINGLE chokepoint every LIRIL outbound
request must go through. Nothing else in tools/ should call
urllib.request directly — they should call liril_network_reach.request()
which enforces:

  (1) ALLOWLIST by host pattern (data/liril_network_allowlist.txt)
      Default includes: api.github.com, raw.githubusercontent.com,
      anthropic.com, openai.com, msrc.microsoft.com,
      nvd.nist.gov, 127.0.0.1, localhost
  (2) DENYLIST (hard-coded) for classes of destinations:
      - No IP-literal URLs (forces DNS → resolves to hostnames in logs)
      - No private-range IPs except explicitly 127.0.0.1 / localhost
      - No URL-shorteners (bit.ly, t.co, tinyurl) — they hide
        destinations from allowlist matching
      - Auth endpoints are allowed BUT the bearer token is never logged
  (3) RATE LIMIT — default 30 requests per minute per host.
  (4) Cap#10 FSE gate — refused when current_level() >= 3.
  (5) Auth header sanitisation in audit logs — "Authorization: Bearer
      ...XYZ" becomes "Authorization: Bearer <redacted:32>".

Response bodies are NOT persisted to the audit log (privacy + size).
Only metadata: host, path, method, status code, response size, duration.

Mechanism
---------
urllib.request (stdlib — no external deps) with custom OpenerDirector
that enforces everything above. TLS verification always on, timeouts
always set. 302/301 follows re-run allowlist check on the redirect
target (critical — bit.ly-style destinations must not be reachable via
redirect chain from an allowlisted domain).

NATS surface
------------
  tenet5.liril.network.request   — RPC subject other caps can request() on
  tenet5.liril.network.audit     — every request + metadata after it completes
  tenet5.liril.network.metrics   — 60-s snapshot (request count, by host,
                                   error count, rate-limit rejections)

CLI
---
  --get URL                    Fetch a URL (GET)
  --post URL [--data JSON]     POST JSON data
  --request "METHOD URL"       Any method
  --list-allowlist             Print current allowlist
  --list-denylist              Print hard-coded denylist patterns
  --snapshot                   Publish a metrics snapshot
  --daemon                     Run the NATS RPC responder
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import ipaddress
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
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

NATS_URL         = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
REQUEST_SUBJECT  = "tenet5.liril.network.request"
AUDIT_SUBJECT    = "tenet5.liril.network.audit"
METRICS_SUBJECT  = "tenet5.liril.network.metrics"

DEFAULT_TIMEOUT_SEC       = 15.0
MAX_RESPONSE_BYTES        = 2 * 1024 * 1024   # 2 MB hard cap
RATE_LIMIT_PER_MIN        = int(os.environ.get("LIRIL_NET_RATE", "30"))
USER_AGENT                = "liril/1.0 (+https://tenet-5.github.io)"
EXEC_GATE                 = os.environ.get("LIRIL_EXECUTE", "1") == "1"  # default ON — network is RPC

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWLIST_FILE = DATA_DIR / "liril_network_allowlist.txt"
AUDIT_LOG      = DATA_DIR / "liril_network_audit.jsonl"
STATE_DB       = DATA_DIR / "liril_network_state.sqlite"

# Hard-coded denylist — no override via allowlist file
_DENIED_HOST_PATTERNS = (
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly",
    "buff.ly", "is.gd", "lnkd.in", "cutt.ly",
)
_DENIED_SUFFIXES = ()  # place for specific TLD bans if Daniel adds them

# Default allowlist seeded on first run
_DEFAULT_ALLOWLIST = """# LIRIL outbound network allowlist
# One host pattern per line. Supports fnmatch wildcards (*, ?).
# Lines starting with # are comments.
#
# Security + update sources
api.github.com
raw.githubusercontent.com
objects.githubusercontent.com
codeload.github.com
msrc.microsoft.com
*.msrc.microsoft.com
nvd.nist.gov
services.nvd.nist.gov
update.microsoft.com
*.update.microsoft.com
windowsupdate.microsoft.com
*.windowsupdate.microsoft.com
# Anthropic + OpenAI (reference inference, not fallback)
api.anthropic.com
api.openai.com
# Hugging Face (model metadata)
huggingface.co
*.huggingface.co
# NVIDIA
developer.nvidia.com
docs.nvidia.com
# Localhost + private loopback
127.0.0.1
localhost
"""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_allowlist() -> None:
    if not ALLOWLIST_FILE.exists():
        ALLOWLIST_FILE.write_text(_DEFAULT_ALLOWLIST, encoding="utf-8")


def _load_allowlist() -> list[str]:
    _ensure_allowlist()
    patterns: list[str] = []
    try:
        for line in ALLOWLIST_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                patterns.append(s.lower())
    except Exception:
        pass
    return patterns


def _host_matches(host: str, patterns: list[str]) -> bool:
    h = (host or "").strip().lower()
    for p in patterns:
        if fnmatch.fnmatch(h, p):
            return True
    return False


def _is_denied_host(host: str) -> tuple[bool, str]:
    h = (host or "").strip().lower()
    for p in _DENIED_HOST_PATTERNS:
        if fnmatch.fnmatch(h, p):
            return True, f"url shortener: {p}"
    for s in _DENIED_SUFFIXES:
        if h.endswith(s):
            return True, f"denied suffix: {s}"
    # IP-literal refusal (except loopback)
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback:
            return False, ""
        if ip.is_private or ip.is_reserved or ip.is_link_local:
            return True, f"private/reserved IP: {ip}"
        return True, f"IP-literal URLs not allowed: {ip}"
    except ValueError:
        pass  # hostname, not IP — fine
    return False, ""


# ─────────────────────────────────────────────────────────────────────
# RATE LIMIT
# ─────────────────────────────────────────────────────────────────────

_rate_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=120))


def _rate_allow(host: str) -> bool:
    """Return True if this host is within rate limit (sliding 60-s window)."""
    q = _rate_windows[host]
    now = time.time()
    while q and (now - q[0]) > 60.0:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        return False
    q.append(now)
    return True


# ─────────────────────────────────────────────────────────────────────
# AUTH HEADER SANITISATION
# ─────────────────────────────────────────────────────────────────────

_AUTH_HEADER_RE = re.compile(r"(bearer|basic|token)\s+(\S+)", re.IGNORECASE)


def _sanitise_headers(headers: dict | None) -> dict:
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key", "x-auth-token", "cookie"):
            out[k] = "<redacted>"
            continue
        sv = str(v)
        m = _AUTH_HEADER_RE.search(sv)
        if m:
            out[k] = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} <redacted:{len(m.group(2))}>", sv)
        else:
            out[k] = sv
    return out


# ─────────────────────────────────────────────────────────────────────
# FSE GATE
# ─────────────────────────────────────────────────────────────────────

def _fse_safe() -> tuple[bool, int]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        return _fse.is_safe_to_execute(), _fse.current_level()
    except Exception:
        return True, 0


# ─────────────────────────────────────────────────────────────────────
# AUDIT + STATE
# ─────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(STATE_DB), timeout=5)
    c.execute("""
        CREATE TABLE IF NOT EXISTS audit (
            ts           REAL PRIMARY KEY,
            host         TEXT NOT NULL,
            method       TEXT NOT NULL,
            path         TEXT NOT NULL,
            status       INTEGER,
            bytes        INTEGER,
            duration_ms  INTEGER,
            error        TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_host ON audit(host)")
    c.commit()
    return c


def _audit_write(row: dict) -> None:
    try:
        c = _db()
        try:
            c.execute(
                "INSERT OR REPLACE INTO audit(ts, host, method, path, status, bytes, duration_ms, error) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    row.get("ts_unix") or time.time(),
                    row.get("host", ""),
                    row.get("method", ""),
                    row.get("path", ""),
                    int(row.get("status") or 0) or None,
                    int(row.get("bytes") or 0) or None,
                    int(row.get("duration_ms") or 0) or None,
                    row.get("error"),
                ),
            )
            c.commit()
        finally:
            c.close()
    except Exception:
        pass
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# REQUEST CORE
# ─────────────────────────────────────────────────────────────────────

class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check allowlist on every redirect. Refuse cross-domain
    redirects to non-allowlisted hosts."""
    def __init__(self, allowlist: list[str]):
        self.allowlist = allowlist

    def http_error_301(self, req, fp, code, msg, headers):
        return self._redirect(req, fp, code, msg, headers,
                               super().http_error_301)

    def http_error_302(self, req, fp, code, msg, headers):
        return self._redirect(req, fp, code, msg, headers,
                               super().http_error_302)

    def http_error_303(self, req, fp, code, msg, headers):
        return self._redirect(req, fp, code, msg, headers,
                               super().http_error_303)

    def http_error_307(self, req, fp, code, msg, headers):
        return self._redirect(req, fp, code, msg, headers,
                               super().http_error_307)

    def _redirect(self, req, fp, code, msg, headers, base):
        loc = headers.get("Location") or headers.get("location") or ""
        try:
            target_host = urllib.parse.urlparse(loc).hostname or ""
        except Exception:
            target_host = ""
        if target_host and not _host_matches(target_host, self.allowlist):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"refusing redirect to non-allowlisted host: {target_host}",
                headers, fp,
            )
        denied, reason = _is_denied_host(target_host)
        if denied:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"refusing redirect to denied host: {target_host} ({reason})",
                headers, fp,
            )
        return base(req, fp, code, msg, headers)


def request(
    method: str,
    url: str,
    data: bytes | str | dict | None = None,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    allow_on_fse: bool = False,
) -> dict:
    """Primary entry point. Returns a dict:
        {ok, status, bytes, body_b64 (or str), url, host, path, error, duration_ms}
    """
    t0 = time.time()
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return _result(False, url=url, error=f"bad url: {e}", t0=t0)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    method = (method or "GET").upper()

    # Deny checks first
    denied, deny_reason = _is_denied_host(host)
    if denied:
        r = _result(False, url=url, host=host, path=path, method=method,
                    error=f"denied: {deny_reason}", t0=t0)
        _publish_and_audit(r)
        return r

    # Allowlist
    allowlist = _load_allowlist()
    if not _host_matches(host, allowlist):
        r = _result(False, url=url, host=host, path=path, method=method,
                    error="host not in allowlist", t0=t0)
        _publish_and_audit(r)
        return r

    # FSE gate
    if not allow_on_fse:
        ok_exec, lvl = _fse_safe()
        if not ok_exec:
            r = _result(False, url=url, host=host, path=path, method=method,
                        error=f"refused_by_failsafe level={lvl}", t0=t0)
            _publish_and_audit(r)
            return r

    # Rate limit
    if not _rate_allow(host):
        r = _result(False, url=url, host=host, path=path, method=method,
                    error=f"rate_limited ({RATE_LIMIT_PER_MIN}/min)", t0=t0)
        _publish_and_audit(r)
        return r

    # Build + send
    if isinstance(data, dict):
        data_bytes = json.dumps(data).encode("utf-8")
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(data, str):
        data_bytes = data.encode("utf-8")
        hdrs = dict(headers or {})
    elif isinstance(data, (bytes, bytearray)):
        data_bytes = bytes(data)
        hdrs = dict(headers or {})
    else:
        data_bytes = None
        hdrs = dict(headers or {})

    hdrs.setdefault("User-Agent", USER_AGENT)
    hdrs.setdefault("Accept", "application/json, */*;q=0.5")

    req = urllib.request.Request(url, data=data_bytes, headers=hdrs, method=method)
    opener = urllib.request.build_opener(_StrictRedirectHandler(allowlist))

    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            truncated = len(raw) > MAX_RESPONSE_BYTES
            if truncated:
                raw = raw[:MAX_RESPONSE_BYTES]
            # Try to decode as utf-8 text; fall back to base64
            try:
                body = raw.decode("utf-8")
                body_is_text = True
            except Exception:
                import base64
                body = base64.b64encode(raw).decode("ascii")
                body_is_text = False
            r = _result(True, url=url, host=host, path=path, method=method,
                        status=status, bytes=len(raw), t0=t0,
                        body=body, body_is_text=body_is_text,
                        truncated=truncated,
                        headers_sent=_sanitise_headers(hdrs))
    except urllib.error.HTTPError as e:
        try:
            err_body = (e.read() or b"")[:2048].decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        r = _result(False, url=url, host=host, path=path, method=method,
                    status=e.code, bytes=len(err_body.encode("utf-8", errors="replace")),
                    t0=t0, error=f"HTTPError {e.code}: {e.reason}",
                    body=err_body, body_is_text=True,
                    headers_sent=_sanitise_headers(hdrs))
    except urllib.error.URLError as e:
        r = _result(False, url=url, host=host, path=path, method=method,
                    t0=t0, error=f"URLError: {e.reason}",
                    headers_sent=_sanitise_headers(hdrs))
    except socket.timeout:
        r = _result(False, url=url, host=host, path=path, method=method,
                    t0=t0, error=f"timeout after {timeout:.1f}s",
                    headers_sent=_sanitise_headers(hdrs))
    except Exception as e:
        r = _result(False, url=url, host=host, path=path, method=method,
                    t0=t0, error=f"{type(e).__name__}: {e}",
                    headers_sent=_sanitise_headers(hdrs))

    _publish_and_audit(r)
    return r


def _result(ok: bool, **kw) -> dict:
    t0 = kw.pop("t0", time.time())
    duration_ms = int((time.time() - t0) * 1000)
    return {
        "ok":          ok,
        "ts":          _utc(),
        "ts_unix":     time.time(),
        "duration_ms": duration_ms,
        **kw,
    }


def _publish_and_audit(r: dict) -> None:
    # Audit log — never includes body
    audit_row = {k: v for k, v in r.items() if k not in ("body",)}
    _audit_write(audit_row)
    # NATS — skip body too for audit subject
    try:
        asyncio.run(_publish_audit(audit_row))
    except Exception:
        pass


async def _publish_audit(row: dict) -> None:
    try:
        import nats as _nats
        nc = await _nats.connect(NATS_URL, connect_timeout=2)
    except Exception:
        return
    try:
        await nc.publish(AUDIT_SUBJECT, json.dumps(row, default=str).encode())
    finally:
        try: await nc.drain()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# NATS RPC DAEMON
# ─────────────────────────────────────────────────────────────────────

async def _rpc_handler(msg) -> None:
    try:
        d = json.loads(msg.data.decode())
    except Exception as e:
        if msg.reply:
            await msg._client.publish(msg.reply,
                json.dumps({"ok": False, "error": f"bad json: {e}"}).encode())
        return
    method = d.get("method", "GET")
    url    = d.get("url", "")
    data   = d.get("data")
    headers = d.get("headers")
    timeout = float(d.get("timeout", DEFAULT_TIMEOUT_SEC))
    # Drop body on wire for audit subscribers; keep it in RPC reply
    r = request(method, url, data=data, headers=headers, timeout=timeout)
    if msg.reply:
        try:
            await msg._client.publish(msg.reply,
                json.dumps(r, default=str).encode()[:1024 * 1024])
        except Exception:
            pass


async def _daemon() -> None:
    import nats as _nats
    print(f"[NET] daemon starting — RPC on {REQUEST_SUBJECT}")
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        print(f"[NET] NATS unavailable: {e!r}")
        return
    await nc.subscribe(REQUEST_SUBJECT, cb=_rpc_handler)

    last_metric = 0.0
    try:
        while True:
            # Periodic metrics
            if time.time() - last_metric >= 60:
                try:
                    c = _db()
                    try:
                        cutoff = time.time() - 3600
                        n = c.execute(
                            "SELECT COUNT(*) FROM audit WHERE ts >= ?", (cutoff,)
                        ).fetchone()[0]
                        n_err = c.execute(
                            "SELECT COUNT(*) FROM audit WHERE ts >= ? AND error IS NOT NULL",
                            (cutoff,)
                        ).fetchone()[0]
                        by_host = {h: cnt for h, cnt in c.execute(
                            "SELECT host, COUNT(*) FROM audit WHERE ts >= ? "
                            "GROUP BY host ORDER BY 2 DESC LIMIT 10", (cutoff,)
                        )}
                    finally:
                        c.close()
                    snap = {
                        "ts":           _utc(),
                        "window_sec":   3600,
                        "requests":     n,
                        "errors":       n_err,
                        "by_host":      by_host,
                        "rate_limit":   RATE_LIMIT_PER_MIN,
                    }
                    await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
                except Exception as e:
                    print(f"[NET] metrics publish failed: {e!r}")
                last_metric = time.time()
            await asyncio.sleep(5)
    finally:
        try: await nc.drain()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Outbound Network Reach")
    ap.add_argument("--get",              type=str, metavar="URL", help="GET a URL")
    ap.add_argument("--post",             type=str, metavar="URL", help="POST to a URL")
    ap.add_argument("--request",          type=str, metavar="METHOD URL",
                    help='e.g. "PUT https://api.example.com/thing"')
    ap.add_argument("--data",             type=str, default=None,
                    help="JSON body for --post/--request")
    ap.add_argument("--timeout",          type=float, default=DEFAULT_TIMEOUT_SEC)
    ap.add_argument("--header",           action="append", default=[],
                    help='Extra header, e.g. --header "Accept: application/vnd.github.v3+json"')
    ap.add_argument("--list-allowlist",   action="store_true")
    ap.add_argument("--list-denylist",    action="store_true")
    ap.add_argument("--snapshot",         action="store_true")
    ap.add_argument("--daemon",           action="store_true")
    args = ap.parse_args()

    if args.list_allowlist:
        for p in _load_allowlist():
            print(p)
        return 0
    if args.list_denylist:
        print("# URL shorteners:")
        for p in _DENIED_HOST_PATTERNS:
            print(f"  {p}")
        print("# Also denied: IP-literal URLs (except 127.0.0.1), private/reserved IPs")
        return 0

    extra_headers: dict = {}
    for h in args.header or []:
        if ":" in h:
            k, _, v = h.partition(":")
            extra_headers[k.strip()] = v.strip()

    if args.get:
        r = request("GET", args.get, headers=extra_headers, timeout=args.timeout)
        print(json.dumps({k: v for k, v in r.items() if k != "body"}, indent=2, default=str))
        if "body" in r:
            body = r["body"]
            if isinstance(body, str) and r.get("body_is_text"):
                print("---BODY---")
                print(body[:4000])
        return 0 if r["ok"] else 1

    if args.post or args.request:
        if args.request:
            parts = args.request.split(None, 1)
            if len(parts) != 2:
                print("--request expects 'METHOD URL'")
                return 2
            method, url = parts
        else:
            method, url = "POST", args.post
        data = None
        if args.data:
            try:
                data = json.loads(args.data)
            except Exception:
                data = args.data  # treat as raw body
        r = request(method, url, data=data, headers=extra_headers, timeout=args.timeout)
        print(json.dumps({k: v for k, v in r.items() if k != "body"}, indent=2, default=str))
        if "body" in r and r.get("body_is_text"):
            print("---BODY---")
            print(r["body"][:4000])
        return 0 if r["ok"] else 1

    if args.snapshot:
        async def run():
            import nats as _nats
            try:
                nc = await _nats.connect(NATS_URL, connect_timeout=3)
            except Exception:
                print("NATS unavailable")
                return
            try:
                c = _db()
                try:
                    cutoff = time.time() - 3600
                    n = c.execute("SELECT COUNT(*) FROM audit WHERE ts >= ?",
                                  (cutoff,)).fetchone()[0]
                    n_err = c.execute("SELECT COUNT(*) FROM audit WHERE ts >= ? "
                                      "AND error IS NOT NULL", (cutoff,)).fetchone()[0]
                    by_host = {h: cnt for h, cnt in c.execute(
                        "SELECT host, COUNT(*) FROM audit WHERE ts >= ? "
                        "GROUP BY host ORDER BY 2 DESC LIMIT 10", (cutoff,))}
                finally:
                    c.close()
                snap = {
                    "ts":         _utc(),
                    "window_sec": 3600,
                    "requests":   n,
                    "errors":     n_err,
                    "by_host":    by_host,
                    "rate_limit": RATE_LIMIT_PER_MIN,
                }
                await nc.publish(METRICS_SUBJECT, json.dumps(snap).encode())
                print(json.dumps(snap, indent=2))
            finally:
                await nc.drain()
        asyncio.run(run())
        return 0

    if args.daemon:
        try:
            asyncio.run(_daemon())
        except KeyboardInterrupt:
            print("\n[NET] daemon stopped")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
