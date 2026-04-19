#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T21:00:00Z | Author: claude_code | Change: NPU health monitor
"""LIRIL NPU health monitor (LIRIL self-request #2).

Every 120 seconds:
  1. Probe tenet5.liril.classify with a canonical test prompt.
  2. Parse domain + confidence + scores from response.
  3. Measure round-trip latency.
  4. Write an observation to data/liril_thoughts.jsonl so
     liril-thinks.html can surface current classifier health.
  5. If latency > 1000 ms OR confidence < 0.10 on a task that
     SHOULD be high-confidence, write an alert.

This addresses LIRIL's second self-requested improvement today:
'Add liril_npu_service health checks to monitor and alert on
NPU classifier performance degradation. Proactive monitoring
will minimize downtime and ensure consistent classification
performance.'

Run as a daemon via pythonw (no console window):
    pythonw tools/liril_npu_health.py

Or one-shot:
    python tools/liril_npu_health.py --once

Exits cleanly on SIGTERM.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
THOUGHTS_PATH = Path(r"E:/TENET-5.github.io/data/liril_thoughts.jsonl")
STATE_PATH = Path(r"E:/S.L.A.T.E/tenet5/data/liril_npu_health_state.json")

# Canonical health-check prompts and expected domains. If the
# classifier drifts, these labels stop matching.
HEALTH_PROBES = [
    {"task": "add reader_guidance field to data json file",
     "expect_any_of": ["ETHICS", "TECHNOLOGY"],
     "expect_confidence_above": 0.15},
    {"task": "write a criminal prosecution Form 2 Information",
     "expect_any_of": ["ETHICS", "REASONING"],
     "expect_confidence_above": 0.15},
    {"task": "benchmark GPU latency with dual RTX 5070",
     "expect_any_of": ["TECHNOLOGY", "SCIENCE"],
     "expect_confidence_above": 0.15},
]

INTERVAL_SECONDS = 120
_stop = False


def _handle_sigterm(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _handle_sigterm)
try:
    signal.signal(signal.SIGINT, _handle_sigterm)
except (AttributeError, ValueError):
    pass


async def _probe_once(nc) -> list[dict]:
    """Fire every HEALTH_PROBES entry and return a structured list."""
    out = []
    for probe in HEALTH_PROBES:
        t0 = time.perf_counter()
        try:
            msg = await nc.request("tenet5.liril.classify",
                                   json.dumps({"task": probe["task"]}).encode(),
                                   timeout=6)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            data = json.loads(msg.data.decode())
            domain = data.get("domain")
            confidence = float(data.get("confidence", 0.0))
            scores = data.get("scores", {})
            expected = probe["expect_any_of"]
            conf_ok = confidence >= probe["expect_confidence_above"]
            domain_ok = domain in expected
            out.append({
                "task": probe["task"][:50],
                "domain": domain,
                "expected": expected,
                "confidence": round(confidence, 3),
                "domain_ok": domain_ok,
                "confidence_ok": conf_ok,
                "latency_ms": latency_ms,
                "scores": {k: round(v, 3) for k, v in (scores or {}).items()},
            })
        except Exception as e:
            out.append({
                "task": probe["task"][:50],
                "error": f"{type(e).__name__}: {e}",
                "domain_ok": False,
                "confidence_ok": False,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
            })
    return out


def _append_thought(kind: str, summary: str, severity: str, **extra) -> None:
    rec = {
        "ts":       int(time.time()),
        "kind":     kind,
        "source":   "npu_health",
        "summary":  summary,
        "severity": severity,
        **extra,
    }
    try:
        THOUGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with THOUGHTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _persist_state(results: list[dict]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "last_probe_utc": int(time.time()),
            "results": results,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _evaluate(results: list[dict]) -> tuple[str, str]:
    """Return (severity, summary) for the overall health."""
    n = len(results)
    errs = sum(1 for r in results if "error" in r)
    domain_fails = sum(1 for r in results if not r.get("domain_ok"))
    conf_fails = sum(1 for r in results if not r.get("confidence_ok"))
    slow = sum(1 for r in results if (r.get("latency_ms") or 0) > 1000)
    max_lat = max((r.get("latency_ms") or 0) for r in results) if results else 0

    if errs == n:
        return "high", f"NPU classifier DOWN — all {n} probes errored"
    if errs > 0 or domain_fails > n // 2 or slow >= 2:
        return "high", (f"NPU DEGRADED — {errs} errors, {domain_fails}/{n} wrong domain, "
                       f"{slow}/{n} slow (max {max_lat}ms)")
    if conf_fails > 0:
        return "medium", (f"NPU confidence low on {conf_fails}/{n} probes — "
                         f"max {max_lat}ms")
    return "low", f"NPU healthy — {n} probes, max latency {max_lat}ms"


async def _one_probe_cycle() -> None:
    try:
        import nats
    except ImportError:
        print("nats-py not installed", file=sys.stderr)
        return
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=5)
    except Exception as e:
        _append_thought("alert",
                        f"NPU health monitor cannot reach NATS at {NATS_URL}: {e!r}",
                        severity="high")
        return
    try:
        results = await _probe_once(nc)
        severity, summary = _evaluate(results)
        kind = "alert" if severity == "high" else ("watch" if severity == "medium" else "observation")
        _append_thought(kind, summary, severity, probes=len(results),
                        errors=sum(1 for r in results if "error" in r),
                        max_latency_ms=max((r.get("latency_ms") or 0) for r in results))
        _persist_state(results)
    finally:
        await nc.drain()


async def main_async(once: bool) -> None:
    if once:
        await _one_probe_cycle()
        return
    _append_thought("observation",
                    f"NPU health monitor boot — probing every {INTERVAL_SECONDS}s",
                    severity="low")
    while not _stop:
        try:
            await _one_probe_cycle()
        except Exception as e:
            _append_thought("alert",
                            f"NPU health cycle raised: {e!r}",
                            severity="medium")
        # Sleep in small chunks so SIGTERM is responsive
        for _ in range(INTERVAL_SECONDS):
            if _stop:
                break
            await asyncio.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="LIRIL NPU classifier health monitor")
    ap.add_argument("--once", action="store_true", help="single probe + exit")
    args = ap.parse_args()
    asyncio.run(main_async(once=args.once))


if __name__ == "__main__":
    main()
