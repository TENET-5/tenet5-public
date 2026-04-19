#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T21:20:00Z | Author: claude_code | Change: NPU trainer from memory
"""Train the NPU classifier from LIRIL's own memory of schema_ok tasks.

Problem statement: liril_npu_health caught real classifier drift —
2 of 3 canonical prompts classified wrongly. The classifier keeps
accumulating samples slowly (5000+ after two days). We can accelerate
convergence by feeding it the 353+ labelled task entries that the
dev-team has already produced during 2026-04-18's session.

Every memory entry has:
  * task title + acceptance (the INPUT the classifier sees at runtime)
  * axis_domain (the correct LABEL — set by whoever authored the task)
  * schema_ok = True (quality gate — only well-structured cycles)

Submitting these as training samples teaches the NPU classifier what
each axis actually looks like in real TENET5 tasks.

Run:
  python tools/liril_train_from_memory.py              # single pass
  python tools/liril_train_from_memory.py --dry-run    # count without publishing
  python tools/liril_train_from_memory.py --retrain-after    # force NPU retrain after feed
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
DB_PATH = Path(r"E:/S.L.A.T.E/tenet5/data/liril_memory.sqlite")


def _load_unique_samples() -> list[tuple[str, str]]:
    """Return list of (task_text, axis_domain) pairs, deduped."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT title, axis_domain, acceptance "
            "FROM entries WHERE schema_ok=1 AND axis_domain != '' "
            "ORDER BY mtime DESC"
        ).fetchall()
    finally:
        conn.close()
    seen = set()
    out = []
    for title, axis, acceptance in rows:
        if not title or not axis:
            continue
        # Build a robust training sample: title + first sentence of acceptance.
        first_sentence = (acceptance or "").split(".")[0].strip()[:240]
        txt = (title + ". " + first_sentence).strip(". ").strip()
        key = (txt.lower(), axis.upper())
        if key in seen:
            continue
        seen.add(key)
        out.append((txt, axis.upper()))
    return out


async def _publish_batch(samples: list[tuple[str, str]]) -> dict:
    try:
        import nats
    except ImportError:
        return {"error": "nats-py not installed"}

    nc = await nats.connect(NATS_URL, connect_timeout=5)
    ok = 0
    errs = 0
    first_err = None
    try:
        for text, domain in samples:
            payload = json.dumps({
                "task": text,
                "domain": domain,
                "source": "liril_train_from_memory",
            }).encode()
            try:
                # Use publish (fire-and-forget) — train subject doesn't need reply
                await nc.publish("tenet5.liril.train", payload)
                ok += 1
            except Exception as e:
                errs += 1
                if first_err is None:
                    first_err = f"{type(e).__name__}: {e}"
        await nc.flush(timeout=5)
    finally:
        await nc.drain()
    return {"submitted": ok, "errors": errs, "first_err": first_err}


async def _force_retrain() -> dict:
    try:
        import nats
    except ImportError:
        return {"error": "nats-py not installed"}
    nc = await nats.connect(NATS_URL, connect_timeout=5)
    try:
        msg = await nc.request("tenet5.liril.retrain",
                               json.dumps({"source": "liril_train_from_memory"}).encode(),
                               timeout=30)
        return json.loads(msg.data.decode())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await nc.drain()


def main():
    ap = argparse.ArgumentParser(description="Train LIRIL NPU from dev-team memory")
    ap.add_argument("--dry-run", action="store_true",
                    help="count samples without publishing")
    ap.add_argument("--retrain-after", action="store_true",
                    help="force NPU retrain after samples are fed")
    args = ap.parse_args()

    samples = _load_unique_samples()
    print(f"Loaded {len(samples)} unique (task, domain) samples from memory.")

    # Per-domain summary
    from collections import Counter
    by_axis = Counter(d for _, d in samples)
    for axis, count in sorted(by_axis.items(), key=lambda x: -x[1]):
        print(f"  {axis:15s} {count:5d} samples")

    if args.dry_run:
        print("\n[dry-run] not publishing")
        return 0

    if not samples:
        print("No samples to submit.")
        return 1

    result = asyncio.run(_publish_batch(samples))
    print(f"\nSubmitted: {result.get('submitted')}  errors: {result.get('errors')}")
    if result.get("first_err"):
        print(f"First error: {result['first_err']}")

    if args.retrain_after:
        print("\nRequesting NPU retrain...")
        r = asyncio.run(_force_retrain())
        print(f"Retrain response: {json.dumps(r, indent=2)[:400]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
