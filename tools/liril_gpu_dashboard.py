#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T22:45:00Z | Author: claude_code | Change: real-time GPU dashboard (LIRIL advise)
"""Real-time LIRIL infra dashboard on :8091.

Spun up on LIRIL's own advice (2026-04-18 via tenet5.liril.advise):
  "BUILD: Real-time GPU dashboard on :8091 — VRAM, tok/s, inference
   queue, agent status"

Serves:
  /            — HTML dashboard that polls /state every 2s
  /state       — JSON snapshot of current infra state
  /state/raw   — same JSON, for debugging

What it shows (no external deps — stdlib only):
  GPUs:
    - name, VRAM used/free, utilization%, temperature
    (via nvidia-smi JSON query)
  llama-servers:
    - port, model name, /health status, last probe latency
  LIRIL NATS subjects:
    - recent request count (from NemoServer stats)
  Dev-team:
    - last N cycle outcomes from transcripts
    - rolling PASS rate
  NPU classifier:
    - training samples, classified count (from tenet5.liril.stats)

Run (with popup_hunter-friendly flag):
  python tools/liril_gpu_dashboard.py --port 8091
or as pythonw.exe to avoid console window:
  pythonw tools/liril_gpu_dashboard.py --port 8091
"""
from __future__ import annotations

import argparse
import glob
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
import subprocess
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# No console window on Windows when we run subprocess checks
_CNW = 0x08000000 if os.name == "nt" else 0

LOG_DIR = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")
GPU_PORTS = [8082, 8083]
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


def _nvidia_smi() -> list[dict]:
    """Query nvidia-smi for GPU state. Returns list of dicts per GPU."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,"
             "utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=_CNW,
        )
        gpus = []
        for line in r.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            gpus.append({
                "index":       int(parts[0]),
                "name":        parts[1],
                "vram_total":  int(parts[2]),
                "vram_used":   int(parts[3]),
                "vram_free":   int(parts[4]),
                "utilization": int(parts[5]),
                "temperature": int(parts[6]),
            })
        return gpus
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]


def _llama_server_state(port: int) -> dict:
    """Probe a llama-server instance for health + loaded model."""
    out: dict = {"port": port, "status": "unknown", "model": None, "latency_ms": None}
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            out["status"] = body.get("status", "unknown")
    except Exception as e:
        out["status"] = f"down ({type(e).__name__})"
        out["latency_ms"] = int((time.time() - t0) * 1000)
        return out
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            out["model"] = body.get("data", [{}])[0].get("id", "?")
    except Exception:
        pass
    out["latency_ms"] = int((time.time() - t0) * 1000)
    return out


def _recent_cycle_summary(n: int = 30) -> dict:
    """Read the N most-recent dev-team transcripts. Return pass/fail counts
    and per-cycle list with id, status, verdict.
    """
    if not LOG_DIR.exists():
        return {"cycles": [], "total": 0, "pass_rate": 0.0}
    files = sorted(LOG_DIR.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    cycles: list[dict] = []
    ok = 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        eng = next((r for r in d.get("roles", []) if r.get("role") == "engineer"), {})
        v = d.get("verdict")
        cycles.append({
            "id":        d.get("task", {}).get("id", "?"),
            "title":     (d.get("task", {}).get("title") or "")[:80],
            "axis":      d.get("task", {}).get("axis_domain", "?"),
            "status":    d.get("status", "?"),
            "verdict":   v,
            "engineer_chars": len(eng.get("text") or ""),
            "when":      int(f.stat().st_mtime),
        })
        if v == "PASS":
            ok += 1
    return {"cycles": cycles, "total": len(cycles),
            "pass_count": ok,
            "pass_rate": round(ok / len(cycles), 3) if cycles else 0.0}


def _liril_stats() -> dict:
    """Query LIRIL NPU service for classifier stats. Best-effort — returns
    empty dict if NATS unreachable or subject unanswered.
    """
    try:
        import asyncio
        import nats  # type: ignore
    except ImportError:
        return {"error": "nats-py missing"}

    async def _q():
        nc = await nats.connect(NATS_URL, connect_timeout=3)
        try:
            msg = await nc.request(
                "tenet5.liril.advise",
                json.dumps({"agent": "gpu_dashboard", "context": "poll"}).encode(),
                timeout=5,
            )
            return json.loads(msg.data.decode())
        finally:
            await nc.drain()

    try:
        res = asyncio.run(_q())
        # Trim for dashboard display
        return {
            "classifier_samples": res.get("training_samples"),
            "classified":         res.get("stats", {}).get("classified"),
            "routed":             res.get("stats", {}).get("routed"),
            "nats_connections":   res.get("live_state", {}).get("nats_connections"),
            "recommendations":    res.get("recommendations", [])[:3],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _profile_stats() -> dict:
    """Pull per-role profiling stats from the runtime profiler module.
    Direct import — no NATS round-trip needed since both live in the
    same host process namespace.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        import liril_runtime_profiler as p
        # Cheap — reads sqlite
        return {
            "per_role":  p.per_role_stats(hours=24),
            "summary":   p.summary(),
            "slowest":   p.slowest_cycles(5),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _observer_state() -> dict:
    """Pull metrics + recent events from the observer daemon at :8092.
    Separate process, so we hit HTTP rather than direct import.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:8092/metrics", timeout=2) as resp:
            metrics = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen("http://127.0.0.1:8092/recent?limit=20", timeout=2) as resp:
            recent = json.loads(resp.read().decode("utf-8"))
        return {"metrics": metrics, "recent": recent}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _snapshot() -> dict:
    return {
        "ts":           int(time.time()),
        "gpus":         _nvidia_smi(),
        "llama_servers": [_llama_server_state(p) for p in GPU_PORTS],
        "dev_team":     _recent_cycle_summary(30),
        "liril":        _liril_stats(),
        "profile":      _profile_stats(),
        "observer":     _observer_state(),
    }


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LIRIL Infra Dashboard :8091</title>
<style>
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background:#0a0e14; color:#c9d1d9; margin:0; padding:1.5rem; }
  h1 { color:#a78bfa; font-size:1.1rem; margin:0 0 1rem; letter-spacing:0.06em; text-transform:uppercase; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:1rem; }
  .card { background:#12161c; border:1px solid #1f2732; border-radius:8px; padding:1rem 1.2rem; }
  .card h2 { font-size:0.75rem; color:#6ea8f7; margin:0 0 0.7rem; letter-spacing:0.1em; text-transform:uppercase; }
  .row { display:flex; justify-content:space-between; padding:0.25rem 0; font-size:0.86rem; border-bottom:1px solid #141820; }
  .row:last-child { border-bottom:none; }
  .k { color:#8899a6; }
  .v { color:#e6edf3; font-family:'JetBrains Mono','Consolas',monospace; }
  .ok { color:#3fb950; }
  .warn { color:#d29922; }
  .bad { color:#f85149; }
  .bar { height:0.3rem; background:#1b2028; border-radius:2px; margin-top:0.2rem; overflow:hidden; }
  .bar > div { height:100%; background:linear-gradient(90deg,#6ea8f7,#a78bfa); }
  table { width:100%; border-collapse:collapse; font-size:0.78rem; }
  td { padding:0.2rem 0.4rem; border-bottom:1px solid #1b2028; }
  .ts { color:#6e7681; font-size:0.72rem; margin-top:1rem; }
  .pass { color:#3fb950; }
  .fail { color:#f85149; }
  .unp { color:#d29922; }
  .brand { font-size:0.68rem; color:#6e7681; letter-spacing:0.1em; }
</style>
</head>
<body>
<div class="brand">TENET5 · LIRIL · SATOR 5×5 GRID</div>
<h1>LIRIL infra dashboard — live</h1>
<div class="grid" id="grid"></div>
<div class="ts" id="ts">loading…</div>
<script>
async function refresh() {
  try {
    const r = await fetch('/state');
    const d = await r.json();
    const out = [];

    // GPUs
    const gpuRows = (d.gpus || []).map(g => {
      if (g.error) return `<div class="row"><span class="bad">${g.error}</span></div>`;
      const pct = Math.round(g.vram_used / g.vram_total * 100);
      return `
        <div class="row"><span class="k">GPU${g.index}</span><span class="v">${g.name}</span></div>
        <div class="row"><span class="k">VRAM</span><span class="v">${g.vram_used} / ${g.vram_total} MiB (${pct}%)</span></div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <div class="row"><span class="k">util / temp</span><span class="v">${g.utilization}% · ${g.temperature}°C</span></div>
      `;
    }).join('<hr style="border:0;border-top:1px dashed #1f2732;margin:0.6rem 0;">');
    out.push(`<div class="card"><h2>GPUs · NVIDIA-SMI</h2>${gpuRows}</div>`);

    // llama-servers
    const llRows = (d.llama_servers || []).map(s => {
      const cls = s.status === 'ok' ? 'ok' : 'bad';
      return `
        <div class="row"><span class="k">:${s.port}</span><span class="v ${cls}">${s.status}</span></div>
        <div class="row"><span class="k">model</span><span class="v">${s.model || '-'}</span></div>
        <div class="row"><span class="k">probe</span><span class="v">${s.latency_ms ?? '-'} ms</span></div>
      `;
    }).join('<hr style="border:0;border-top:1px dashed #1f2732;margin:0.6rem 0;">');
    out.push(`<div class="card"><h2>llama-servers</h2>${llRows}</div>`);

    // Dev team
    const dt = d.dev_team || { cycles: [] };
    const rows = (dt.cycles || []).slice(0, 12).map(c => {
      const klass = c.verdict === 'PASS' ? 'pass' : (c.verdict ? 'fail' : 'unp');
      const when = new Date(c.when * 1000).toISOString().slice(11, 19);
      return `<tr><td class="${klass}">${c.verdict || '-'}</td><td>${c.id}</td><td>${c.axis}</td><td>${c.engineer_chars}</td><td>${when}</td></tr>`;
    }).join('');
    out.push(`<div class="card"><h2>Dev-team · last ${dt.total} cycles · PASS rate ${Math.round((dt.pass_rate||0)*100)}%</h2>
      <table><thead><tr><td class="k">verdict</td><td class="k">id</td><td class="k">axis</td><td class="k">eng_chars</td><td class="k">time</td></tr></thead>
      <tbody>${rows}</tbody></table></div>`);

    // LIRIL stats
    const l = d.liril || {};
    if (l.error) {
      out.push(`<div class="card"><h2>LIRIL</h2><div class="row"><span class="bad">${l.error}</span></div></div>`);
    } else {
      out.push(`<div class="card"><h2>LIRIL · NPU classifier</h2>
        <div class="row"><span class="k">training_samples</span><span class="v">${l.classifier_samples ?? '-'}</span></div>
        <div class="row"><span class="k">classified (total)</span><span class="v">${l.classified ?? '-'}</span></div>
        <div class="row"><span class="k">routed</span><span class="v">${l.routed ?? '-'}</span></div>
        <div class="row"><span class="k">nats_conns</span><span class="v">${l.nats_connections ?? '-'}</span></div>
        <h2 style="margin-top:0.9rem">LIRIL recommends</h2>
        ${(l.recommendations || []).map(r => `<div class="row"><span class="v">${r}</span></div>`).join('')}
      </div>`);
    }

    // Observer panel (unified activity stream — LIRIL gap #5 feedback loop)
    const ob = d.observer || {};
    if (!ob.error && ob.metrics) {
      const m = ob.metrics;
      const evs = ob.recent || [];
      const evRows = evs.slice(0, 8).map(e => {
        const sub = (e.subject || '').replace('tenet5.liril.', '').replace('tenet5.', '');
        const t = new Date(e.ts * 1000).toLocaleTimeString();
        return `<div class="row" style="font-size:0.72rem"><span class="k">${t} ${sub}</span><span class="v" style="text-align:right;flex:1;padding-left:8px;">${(e.summary || '').substring(0,60)}</span></div>`;
      }).join('');
      out.push(`<div class="card"><h2>Live activity · observer :8092</h2>
        <div class="row"><span class="k">24h pass rate</span><span class="v ${m.pass_rate_last_24h >= 0.4 ? 'ok' : m.pass_rate_last_24h >= 0.3 ? 'warn' : 'bad'}">${Math.round((m.pass_rate_last_24h||0)*100)}% (${m.cycles_passed_last_24h}/${m.cycles_last_24h})</span></div>
        <div class="row"><span class="k">commits 1h / 6h / 24h</span><span class="v">${m.commits_last_hour} / ${m.commits_last_6_hours} / ${m.commits_last_24_hours}</span></div>
        <div class="row"><span class="k">auto / manual (1h)</span><span class="v">${m.autonomous_last_hour} / ${m.manual_last_hour}</span></div>
        <div class="row"><span class="k">QAOA decisions 24h</span><span class="v">${m.qaoa_decisions_last_24h}</span></div>
        <h2 style="margin-top:0.9rem">Recent events</h2>
        ${evRows || '<div class="row"><span class="k">(no events yet)</span></div>'}
      </div>`);
    }

    // Runtime profiler panel (LIRIL gap #1 partial — dev-team role stats)
    const pr = d.profile || {};
    if (!pr.error) {
      const roles = pr.per_role || {};
      const roleRows = Object.keys(roles).map(k => {
        const s = roles[k];
        const okPct = s['schema_ok_%'];
        const okCls = okPct >= 80 ? 'ok' : okPct >= 60 ? 'warn' : 'bad';
        return `<tr>
          <td>${k}</td>
          <td class="v">${s.calls}</td>
          <td class="v">${s.mean_ms}ms</td>
          <td class="v">${s.p90_ms}ms</td>
          <td class="v ${okCls}">${okPct}%</td>
          <td class="v">${s['retried_%']}%</td>
          <td class="v">${s['errors_%']}%</td>
        </tr>`;
      }).join('');
      const sum = pr.summary || {};
      out.push(`<div class="card"><h2>Runtime profile · last 24h per role</h2>
        <table><thead><tr>
          <td class="k">role</td><td class="k">calls</td><td class="k">mean</td>
          <td class="k">p90</td><td class="k">ok</td><td class="k">retry</td><td class="k">err</td>
        </tr></thead><tbody>${roleRows}</tbody></table>
        <div class="row" style="margin-top:0.6rem"><span class="k">total calls</span><span class="v">${sum.calls ?? '-'}</span></div>
        <div class="row"><span class="k">transcripts</span><span class="v">${sum.transcripts ?? '-'}</span></div>
        <h2 style="margin-top:0.9rem">Slowest role calls</h2>
        ${(pr.slowest || []).slice(0, 5).map(row => `<div class="row">
          <span class="k">${row.role} · ${row.task}</span>
          <span class="v">${row.ms}ms</span>
        </div>`).join('')}
      </div>`);
    }

    document.getElementById('grid').innerHTML = out.join('');
    document.getElementById('ts').textContent = 'updated ' + new Date(d.ts * 1000).toLocaleTimeString();
  } catch (e) {
    document.getElementById('ts').textContent = 'error: ' + e;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Silent — don't spam stderr every poll
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/state"):
            snap = _snapshot()
            body = json.dumps(snap, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description="LIRIL real-time infra dashboard")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"LIRIL dashboard → http://{args.host}:{args.port}/")
    print(f"state JSON    → http://{args.host}:{args.port}/state")
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
