#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-16T00:00:00Z
"""liril_quantum_dispatch.py — Session dispatch gate.

Talks to LIRIL (tenet5.liril.advise) and kicks the quantum pipelines
(tenet5.quantum.{capabilities,status,route}) over NATS on 127.0.0.1:4223.

Honors operational rules:
  - NEVER fakes status: every subject is a real NATS request with timeout.
  - LIRIL-first: the first call is tenet5.liril.advise.
  - GPU-only path: quantum.route types fan out to Ising(GPU1) / CUDA-Q(GPU1).
  - SYSTEM_SEED=118400, 127.0.0.1 only, UTF-8, explicit timeouts.

Run from TENET5 venv:
  E:\\S.L.A.T.E\\.venv\\Scripts\\python.exe tools/liril_quantum_dispatch.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

SYSTEM_SEED = 118400
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
AGENT = "claude_code"

if sys.platform == "win32" and sys.version_info < (3, 16):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def _request(nc, subject: str, data: dict, timeout: float = 8.0) -> dict:
    """Real NATS request — returns parsed JSON or an error dict."""
    payload = json.dumps(data).encode("utf-8")
    t0 = time.perf_counter_ns()
    try:
        resp = await nc.request(subject, payload, timeout=timeout)
        elapsed_ms = round((time.perf_counter_ns() - t0) / 1e6, 1)
        try:
            body = json.loads(resp.data.decode("utf-8", errors="replace"))
        except Exception:
            body = {"raw": resp.data.decode("utf-8", errors="replace")[:500]}
        return {"subject": subject, "ok": True, "elapsed_ms": elapsed_ms, "reply": body}
    except Exception as e:
        elapsed_ms = round((time.perf_counter_ns() - t0) / 1e6, 1)
        return {"subject": subject, "ok": False, "elapsed_ms": elapsed_ms, "error": str(e)}


async def main() -> int:
    try:
        import nats
    except ImportError:
        print(json.dumps({"error": "nats-py not installed in venv"}))
        return 2

    print(f"[dispatch] connecting to {NATS_URL} ...")
    try:
        nc = await nats.connect(NATS_URL, name=f"tenet5-{AGENT}-quantum-gate",
                                connect_timeout=5, max_reconnect_attempts=3)
    except Exception as e:
        print(json.dumps({"error": f"nats connect failed: {e}", "url": NATS_URL}))
        return 3
    print(f"[dispatch] connected. seed={SYSTEM_SEED}")

    # ── 1. Announce session to the mesh (AREPO row) ───────────────────────
    await nc.publish(
        "tenet5.arepo.announce",
        json.dumps({"agent": AGENT, "message": "session_start:quantum_pipeline",
                    "ts": time.time(), "seed": SYSTEM_SEED}).encode(),
    )

    # ── 2. Talk to LIRIL: ask for guidance on quantum pipeline next steps ──
    liril = await _request(nc, "tenet5.liril.advise", {
        "agent": AGENT,
        "context": "proceed with quantum development pipelines",
        "seed": SYSTEM_SEED,
    }, timeout=10.0)

    # ── 3. Query the quantum orchestrator capabilities + status ──────────
    caps, status = await asyncio.gather(
        _request(nc, "tenet5.quantum.capabilities", {}, timeout=6.0),
        _request(nc, "tenet5.quantum.status", {}, timeout=6.0),
    )

    # ── 4. Fire full ABCXYZ system analysis through the beam splitter ────
    analysis = await _request(nc, "tenet5.quantum.route",
                              {"type": "abcxyz_analysis"}, timeout=15.0)

    # ── 5. Quick Millennium P-vs-NP benchmark (3 iterations) ─────────────
    pvnp = await _request(nc, "tenet5.quantum.route",
                          {"type": "benchmark", "iterations": 3}, timeout=30.0)

    report = {
        "seed": SYSTEM_SEED,
        "nats_url": NATS_URL,
        "agent": AGENT,
        "ts": time.time(),
        "liril_advise": liril,
        "quantum_capabilities": caps,
        "quantum_status": status,
        "abcxyz_analysis": analysis,
        "pvnp_benchmark": pvnp,
    }

    # Final announce so other agents see we dispatched
    await nc.publish(
        "tenet5.arepo.announce",
        json.dumps({
            "agent": AGENT,
            "message": "quantum_dispatch_complete",
            "liril_ok": liril.get("ok", False),
            "quantum_status_ok": status.get("ok", False),
            "pvnp_ok": pvnp.get("ok", False),
            "ts": time.time(),
        }).encode(),
    )
    await nc.flush(timeout=2.0)
    await nc.drain()

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
