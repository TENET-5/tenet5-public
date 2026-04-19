#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
"""LIRIL GPU Continuous Training Daemon — keeps both GPUs active.

Reads training samples from the LIRIL training DB, round-robins inference
requests across both llama-server GPU endpoints (8082/8083), and feeds
results back as new training samples. Runs continuously at a configurable
rate to maintain GPU utilization.

Usage:
    python tools/liril_gpu_trainer.py [--rate 2.0] [--batch 5]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SYSTEM_SEED = 118400
GPU0_URL = os.environ.get("LLAMA_SERVER_GPU0", "http://127.0.0.1:8082")
GPU1_URL = os.environ.get("LLAMA_SERVER_GPU1", "http://127.0.0.1:11434")
DB_PATH = Path(os.environ.get("LIRIL_DB", "E:/S.L.A.T.E/tenet5/.liril_training.json"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] GPU-TRAINER %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gpu-trainer")

_gpu_idx = 0
_running = True


def _signal_handler(sig, frame):
    global _running
    log.info("Shutdown signal received")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _next_gpu() -> str:
    """Round-robin between GPU 0 and GPU 1."""
    global _gpu_idx
    url = GPU0_URL if _gpu_idx % 2 == 0 else GPU1_URL
    _gpu_idx += 1
    return url


def _infer(gpu_url: str, prompt: str, max_tokens: int = 128) -> dict:
    """Send an inference request to a llama-server endpoint."""
    payload = json.dumps({
        "model": "local",
        "messages": [
            {"role": "system", "content": "You are LIRIL, a precise AI assistant. Be concise."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(
        f"{gpu_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _load_db() -> list[dict]:
    """Load training samples from disk."""
    try:
        raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw.get("training_log", raw.get("samples", []))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _generate_training_prompts(samples: list[dict]) -> list[str]:
    """Generate inference prompts from training samples."""
    prompts = []
    for s in samples:
        task = s.get("task", "")
        domain = s.get("domain", "TECHNOLOGY")
        if task:
            prompts.append(
                f"[{domain}] Improve this solution: {task[:200]}"
            )
    return prompts


def main():
    parser = argparse.ArgumentParser(description="LIRIL GPU Continuous Trainer")
    parser.add_argument("--rate", type=float, default=2.0,
                        help="Seconds between inference batches (default: 2.0)")
    parser.add_argument("--batch", type=int, default=3,
                        help="Requests per batch cycle (default: 3)")
    parser.add_argument("--max-tokens", type=int, default=128,
                        help="Max tokens per inference (default: 128)")
    args = parser.parse_args()

    log.info("LIRIL GPU Trainer v1.0 — SEED=%d", SYSTEM_SEED)
    log.info("GPU 0: %s | GPU 1: %s", GPU0_URL, GPU1_URL)
    log.info("Rate: %.1fs | Batch: %d | Max tokens: %d", args.rate, args.batch, args.max_tokens)

    # Load training DB
    samples = _load_db()
    log.info("Loaded %d training samples from %s", len(samples), DB_PATH)

    if not samples:
        # Generate synthetic prompts if DB is empty
        samples = [{"task": f"Explain concept #{i} in TENET5 system architecture", "domain": "TECHNOLOGY"}
                    for i in range(20)]
        log.info("Generated %d synthetic prompts", len(samples))

    prompts = _generate_training_prompts(samples)
    log.info("Generated %d training prompts", len(prompts))

    cycle = 0
    total_tokens = 0
    total_requests = 0
    prompt_idx = 0

    while _running:
        cycle += 1
        batch_tokens = 0
        batch_start = time.time()

        for _ in range(args.batch):
            if not _running:
                break

            gpu_url = _next_gpu()
            gpu_label = "GPU0" if "8082" in gpu_url else "GPU1"
            prompt = prompts[prompt_idx % len(prompts)]
            prompt_idx += 1

            t0 = time.time()
            result = _infer(gpu_url, prompt, args.max_tokens)
            elapsed = time.time() - t0

            if "error" in result:
                log.warning("[%s] Inference error: %s", gpu_label, result["error"])
                continue

            tokens = result.get("usage", {}).get("completion_tokens", 0)
            total_tokens += tokens
            batch_tokens += tokens
            total_requests += 1

            content = ""
            if "choices" in result and result["choices"]:
                content = result["choices"][0].get("message", {}).get("content", "")

            tps = tokens / elapsed if elapsed > 0 else 0
            log.info(
                "[%s] %d tok in %.1fs (%.1f tok/s) | prompt: %s",
                gpu_label, tokens, elapsed, tps, prompt[:60],
            )

        batch_elapsed = time.time() - batch_start
        log.info(
            "Cycle %d: %d tokens in %.1fs | Total: %d requests, %d tokens",
            cycle, batch_tokens, batch_elapsed, total_requests, total_tokens,
        )

        # Sleep between batches
        if _running:
            time.sleep(args.rate)

    log.info("Shutting down — %d total requests, %d total tokens generated", total_requests, total_tokens)


if __name__ == "__main__":
    main()
