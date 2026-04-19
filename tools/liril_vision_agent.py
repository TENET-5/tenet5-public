#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T00:20:00Z | Author: claude_code | Change: LIRIL vision agent (screenshot diagnosis)
"""LIRIL Vision Agent — multimodal image input for the investigation loop.

This is LIRIL's own #3 ask when I polled her for skills:
  'Multimodal Learning — learn and understand from various data types
   like text, images, and audio simultaneously, allowing a holistic
   understanding of contexts and make more informed decisions.'

It also closes the specific loop that cost us an hour earlier today:
the user sent a screenshot of a broken "red-bubble" visualisation
and I had to reverse-engineer which page produced it from grep
patterns. With this agent, the user can drop a screenshot at a
subject and LIRIL will: (1) extract text via Pillow + tesseract
if available, (2) pull dominant colors / image dimensions, (3) ask
the local LLM 'what might be broken about a UI that renders this?',
(4) return a structured diagnosis.

NATS subject:
  tenet5.liril.vision  — request-reply

Request shape:
  {
    "image_b64":  "<base64 PNG/JPG bytes>",
    "question":   "optional — e.g. 'what UI issue is visible here?'"
  }

Response shape:
  {
    "ok":                bool,
    "image_dim":         [w, h],
    "dominant_colors":   [[r,g,b], ...],
    "ocr_text":          "extracted text …",
    "llm_diagnosis":     "LIRIL's natural-language analysis",
    "processing_ms":     int
  }

CLI modes:
  python tools/liril_vision_agent.py --daemon
  python tools/liril_vision_agent.py --probe path/to/screenshot.png
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
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
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")


# ────────────────────────────────────────────────────────────────
# IMAGE ANALYSIS — pure Pillow for metadata + optional tesseract
# ────────────────────────────────────────────────────────────────

def _analyse_image(image_bytes: bytes) -> dict:
    """Return metadata + OCR text for a given image byte blob.

    Stdlib+Pillow dependencies only. OCR via tesseract if installed.
    Returns {image_dim, dominant_colors, ocr_text, error?}.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return {"error": "Pillow not installed"}

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        return {"error": f"image decode failed: {type(e).__name__}: {e}"}

    w, h = img.size
    out: dict = {"image_dim": [w, h], "format": img.format, "mode": img.mode}

    # Dominant colors via k-means-lite: downsample + count most common RGB buckets
    thumb = img.convert("RGB").resize((80, 80), Image.Resampling.NEAREST)
    from collections import Counter
    buckets: Counter = Counter()
    for px in thumb.getdata():
        r, g, b = px
        # Quantize to 32-step buckets to coalesce near-identical colors
        buckets[(r // 32 * 32, g // 32 * 32, b // 32 * 32)] += 1
    out["dominant_colors"] = [list(rgb) for rgb, _ in buckets.most_common(5)]

    # Is the image mostly red? (simple heuristic)
    red_dom = sum(
        c for rgb, c in buckets.items()
        if rgb[0] > 160 and rgb[1] < 120 and rgb[2] < 120
    )
    total = sum(buckets.values())
    out["red_dominance_pct"] = round(100 * red_dom / max(total, 1), 1)

    # Is it mostly dark background? (dashboard/UI heuristic)
    dark_pixels = sum(
        c for rgb, c in buckets.items()
        if max(rgb) < 64
    )
    out["dark_bg_pct"] = round(100 * dark_pixels / max(total, 1), 1)

    # OCR via tesseract if available
    ocr_text = ""
    ocr_error = None
    try:
        r = subprocess.run(
            ["tesseract", "--version"],
            capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            # Dump image to temp PNG
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                img.convert("RGB").save(tf, format="PNG")
                tmp_path = tf.name
            try:
                r = subprocess.run(
                    ["tesseract", tmp_path, "stdout", "--psm", "6"],
                    capture_output=True, timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
                if r.returncode == 0 and r.stdout:
                    ocr_text = r.stdout.decode("utf-8", errors="replace").strip()
                else:
                    ocr_error = f"tesseract rc={r.returncode}"
            finally:
                try: os.unlink(tmp_path)
                except Exception: pass
    except FileNotFoundError:
        ocr_error = "tesseract not installed on PATH"
    except subprocess.TimeoutExpired:
        ocr_error = "tesseract timeout"
    except Exception as e:
        ocr_error = f"{type(e).__name__}: {e}"

    out["ocr_text"] = ocr_text
    if ocr_error:
        out["ocr_error"] = ocr_error

    return out


# ────────────────────────────────────────────────────────────────
# LLM DIAGNOSIS — feed analysis to Mistral-Nemo via mercury.infer.code
# ────────────────────────────────────────────────────────────────

async def _llm_diagnose(analysis: dict, question: str) -> str:
    try:
        import nats as _nats
    except ImportError:
        return "[llm-unavailable: nats-py missing]"

    # Build a prompt from the analysis metadata
    ocr_text = (analysis.get("ocr_text") or "")[:1500]
    red = analysis.get("red_dominance_pct", 0)
    dark = analysis.get("dark_bg_pct", 0)
    dim = analysis.get("image_dim", [0, 0])

    prompt = (
        f"A user submitted a screenshot for diagnosis. Image metadata:\n"
        f"  dimensions: {dim[0]}x{dim[1]}\n"
        f"  red-dominance: {red}%\n"
        f"  dark-background: {dark}%\n"
        f"  dominant colors (quantized): {analysis.get('dominant_colors', [])}\n\n"
        f"OCR-extracted text from the image:\n"
        f'  """{ocr_text}"""\n\n'
        f"User's question: {question or 'What might be wrong with this UI? What page is this?'}\n\n"
        f"Give a concise 3-sentence diagnosis. If the OCR text is empty, focus on the "
        f"visual heuristics (red dominance, dark background, dimensions). Suggest 1 "
        f"concrete thing the developer should investigate."
    )

    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception as e:
        return f"[llm-unavailable: nats-connect: {e!r}]"
    try:
        msg = await nc.request(
            "mercury.infer.code",
            json.dumps({
                "messages": [
                    {"role": "system", "content": "You are LIRIL, the TENET5 accountability AI. Diagnose UI screenshots in plain English."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 400,
            }).encode(),
            timeout=45,
        )
        d = json.loads(msg.data.decode())
        return (d.get("text") or "").strip()
    except Exception as e:
        return f"[llm-error: {type(e).__name__}: {e}]"
    finally:
        await nc.drain()


# ────────────────────────────────────────────────────────────────
# NATS HANDLER
# ────────────────────────────────────────────────────────────────

async def _handle_vision(msg, nc) -> None:
    t0 = time.time()
    try:
        req = json.loads(msg.data.decode())
    except Exception as e:
        resp = {"ok": False, "error": f"request decode: {type(e).__name__}: {e}"}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(resp).encode())
        return

    image_b64 = req.get("image_b64") or req.get("image")
    question  = req.get("question") or ""

    if not image_b64:
        resp = {"ok": False, "error": "no image_b64 in request"}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(resp).encode())
        return

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception as e:
        resp = {"ok": False, "error": f"base64 decode: {type(e).__name__}: {e}"}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(resp).encode())
        return

    analysis = _analyse_image(image_bytes)
    if "error" in analysis:
        resp = {"ok": False, **analysis}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(resp).encode())
        return

    diagnosis = await _llm_diagnose(analysis, question)

    resp = {
        "ok":              True,
        "image_dim":       analysis.get("image_dim"),
        "dominant_colors": analysis.get("dominant_colors"),
        "red_dominance_pct": analysis.get("red_dominance_pct"),
        "dark_bg_pct":     analysis.get("dark_bg_pct"),
        "ocr_text":        analysis.get("ocr_text", ""),
        "ocr_error":       analysis.get("ocr_error"),
        "llm_diagnosis":   diagnosis,
        "processing_ms":   int((time.time() - t0) * 1000),
    }
    if msg.reply:
        await nc.publish(msg.reply, json.dumps(resp, default=str).encode())


async def daemon() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[VISION] subscribed to tenet5.liril.vision on {NATS_URL}")

    async def _cb(msg):
        await _handle_vision(msg, nc)

    await nc.subscribe("tenet5.liril.vision", cb=_cb)
    # Keep running
    while True:
        await asyncio.sleep(60)


# ────────────────────────────────────────────────────────────────
# CLI PROBE — test against a local image
# ────────────────────────────────────────────────────────────────

def probe(path: str, question: str = "") -> None:
    data = Path(path).read_bytes()
    analysis = _analyse_image(data)
    print("── image analysis ──")
    for k, v in analysis.items():
        if k == "ocr_text":
            print(f"  {k}: {(v or '')[:200]!r}...")
        else:
            print(f"  {k}: {v}")
    diag = asyncio.run(_llm_diagnose(analysis, question))
    print("\n── LLM diagnosis ──")
    print(diag)


def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL vision agent — screenshot diagnosis")
    ap.add_argument("--daemon", action="store_true",
                    help="subscribe to tenet5.liril.vision and serve requests")
    ap.add_argument("--probe",  type=str,
                    help="analyse a local image file path and print diagnosis")
    ap.add_argument("--question", type=str, default="",
                    help="question to ask about the probed image")
    args = ap.parse_args()

    if args.daemon:
        asyncio.run(daemon())
        return 0
    if args.probe:
        probe(args.probe, args.question)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
