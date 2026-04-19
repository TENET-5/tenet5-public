# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T05:00:00Z
"""LIRIL Instagram draft-caption generator.

Produces copy-paste-ready IG captions for each of the top accountability
campaigns. No automation, no credentials, no posting — just text the user
manually pastes into the IG app on their phone.

Safety rules (see feedback_no_lirilclaw.md):
  - No credentials of any kind are read, stored, or transmitted.
  - No Instagram API, web, or browser automation.
  - Output is a plain JSON file on the website that the draft-assist page
    reads. User is the ONLY posting actor.

Each draft:
  - caption  : the 2200-char-max IG caption body, with 3-5 paragraph breaks
  - hashtags : 10-15 Canadian-political hashtags, separated by spaces
  - alt_text : WCAG-compliant alt text for the lead image (<=125 chars)
  - source_link: the tenet-5.github.io URL the caption points readers at
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path

os.environ.setdefault("NATS_URL",      "nats://127.0.0.1:4223")
os.environ.setdefault("NATS_URL_HOST", "nats://127.0.0.1:4223")

import nats  # type: ignore

SITE = Path(r"E:\TENET-5.github.io")
CAMPAIGNS_SRC = SITE / "data" / "grover_target_campaigns.json"
OUT = SITE / "data" / "instagram_drafts.json"

# Curated subset of campaigns appropriate for public IG distribution.
# (Some email-campaign items are too personalized for the public feed.)
IG_READY_IDS = [
    "dual_bridge_sajjan",       # Sajjan arms + CFNIS dual axis
    "dual_bridge_leblanc",      # LeBlanc multi-portfolio
    "dual_bridge_lametti",      # Lametti Justice/AG + MAID
    "rcmp_lucki",               # RCMP Commissioner
    "trudeau_octuple",          # Trudeau 8-axis convergence
    "mendicino_triple",         # Mendicino triple axis
    "blair_dual",               # Blair Public Safety + DND
    "five_chairs_public_safety",# Five chairs concentrating Public Safety
]

HASHTAG_ALT_PROMPT = """/no_think

You are adding hashtags and alt text for an Instagram post about a Canadian
accountability investigation.

The caption is below. DO NOT rewrite the caption. ONLY produce:
  - 10 Canadian-political hashtags on one line (#cdnpoli #parlgc etc)
  - One line of WCAG alt text describing a suitable image (<=120 chars)

Caption:
{caption}

Axis context: {axes}

Respond in EXACTLY this format (no other text):

HASHTAGS: #cdnpoli #parlgc (...8 more tags...)
ALT_TEXT: <one short sentence describing a photo or graphic for this post>

HASHTAGS:"""


IG_PROMPT = """/no_think

You are writing one Instagram caption for a Canadian accountability
investigation account. Audience: engaged citizens, journalists,
parliamentarians, researchers. Platform: Instagram (2200-char cap).

Tone: factual, investigative, non-partisan. Avoid hyperbole. Cite one
specific primary-source fact. Lead with the most important sentence.
Middle: 2-3 paragraphs of detail with one concrete number. End with
the URL tenet-5.github.io and a one-line call to read.

Then, on separate lines, produce:
  HASHTAGS: 10 Canadian-political hashtags separated by spaces (cdnpoli,
    parlgc, etc.). No more than 10. Include axis-specific tags.
  ALT_TEXT: WCAG alt text for an accompanying image, 120 characters max.

Context about the topic:
ID: {id}
TARGET: {target}
AXES: {axes}
SUBJECT: {subject}
SOURCE_URL: {url}
BODY_EXCERPT: {body}

Now write the caption, hashtags, and alt text. Format exactly:

CAPTION:
<the caption, 2-5 paragraphs>

HASHTAGS:
<space-separated hashtags, 10 total>

ALT_TEXT:
<one-line alt text, max 120 chars>
"""


async def call_infer(nc, prompt: str, max_tokens: int = 1200) -> str:
    # The caption alone can be ~500 tokens; hashtags + alt-text add another
    # ~150. Previous max_tokens=700 cut off the HASHTAGS/ALT_TEXT sections
    # on verbose drafts. No aggressive stop sequence — parser handles trailing
    # content safely.
    payload = json.dumps({
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    try:
        msg = await nc.request("mercury.infer", payload, timeout=60)
    except Exception as e:
        return f"[INFER-FAILED: {e!r}]"
    envelope = json.loads(msg.data.decode("utf-8", errors="replace"))
    return envelope.get("text", "")


def parse_sections(text: str) -> dict:
    """Extract CAPTION / HASHTAGS / ALT_TEXT blocks from the model output.

    Tolerant to: case variants (CAPTION/Caption/caption), alt labels
    (HASHTAG/ALT/ALT-TEXT), extra asterisks from markdown emphasis,
    blank lines inside sections, and trailing content after alt_text.
    Hashtags may arrive as a single line or several lines of #tag items.
    """
    out = {"caption": "", "hashtags": "", "alt_text": ""}
    current = None

    def header(L: str) -> str | None:
        # Strip markdown emphasis and colon, return canonical header or None
        stripped = L.strip().lstrip("*").rstrip("*").strip()
        up = stripped.upper()
        # Allow "### CAPTION" style too
        up = up.lstrip("#").strip()
        if up.startswith("CAPTION:") or up == "CAPTION":
            return "caption"
        if up.startswith("HASHTAGS:") or up.startswith("HASHTAG:") or up == "HASHTAGS" or up == "HASHTAG":
            return "hashtags"
        if up.startswith("ALT_TEXT:") or up.startswith("ALT-TEXT:") or up.startswith("ALT TEXT:") or up.startswith("ALT:") \
           or up in ("ALT_TEXT", "ALT-TEXT", "ALT TEXT", "ALT"):
            return "alt_text"
        return None

    hashtag_lines: list[str] = []
    alt_lines: list[str] = []

    for raw in text.splitlines():
        h = header(raw)
        if h is not None:
            current = h
            # Capture trailing inline content after the colon, if any
            if ":" in raw:
                remainder = raw.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                if remainder:
                    if h == "caption":
                        out["caption"] += remainder + "\n"
                    elif h == "hashtags":
                        hashtag_lines.append(remainder)
                    elif h == "alt_text":
                        alt_lines.append(remainder)
            continue
        if current == "caption":
            out["caption"] += raw + "\n"
        elif current == "hashtags":
            if raw.strip():
                hashtag_lines.append(raw.strip())
        elif current == "alt_text":
            if raw.strip():
                alt_lines.append(raw.strip())

    # Join multi-line hashtags/alt_text
    out["hashtags"] = " ".join(hashtag_lines)
    out["alt_text"] = " ".join(alt_lines)

    # Trim caption
    out["caption"] = out["caption"].strip()
    if len(out["caption"]) > 2200:
        out["caption"] = out["caption"][:2190].rstrip() + "…"
    if len(out["alt_text"]) > 125:
        out["alt_text"] = out["alt_text"][:122] + "…"
    # Hashtag cleanup: ensure each starts with #, strip commas/semicolons,
    # filter out stray words (e.g. "and", periods), cap at 12.
    raw_tags = out["hashtags"].replace(",", " ").replace(";", " ").split()
    clean_tags = []
    for t in raw_tags:
        t = t.strip().lstrip("#").rstrip(".,:;!?")
        if t and t.replace("_", "").isalnum() and len(t) <= 40:
            clean_tags.append(t)
    out["hashtags"] = " ".join("#" + t for t in clean_tags[:12])
    return out


def short_axes(axes) -> str:
    if not axes:
        return ""
    return ", ".join(sorted(set(axes)))[:120]


async def draft_all():
    campaigns = json.loads(CAMPAIGNS_SRC.read_text(encoding="utf-8")).get("campaigns", [])
    index = {c.get("id"): c for c in campaigns}

    nc = await nats.connect(os.environ["NATS_URL"])
    drafts = []
    try:
        for cid in IG_READY_IDS:
            camp = index.get(cid)
            if not camp:
                drafts.append({
                    "id": cid, "status": "skip_not_found",
                    "caption": "", "hashtags": "", "alt_text": "",
                })
                continue
            print(f"[DRAFT] {cid}", flush=True)
            body = camp.get("body", "")[:1400]
            prompt = IG_PROMPT.format(
                id=cid,
                target=camp.get("target_name", ""),
                axes=short_axes(camp.get("axis_grover_marked", [])),
                subject=camp.get("subject", ""),
                url=f"https://tenet-5.github.io/#{cid}",
                body=body,
            )
            t0 = time.time()
            text = await call_infer(nc, prompt)
            dt_ms = int((time.time() - t0) * 1000)
            parsed = parse_sections(text)

            # Fallback: if the model stopped before emitting HASHTAGS/ALT_TEXT,
            # do a second focused pass that ONLY asks for hashtags + alt text.
            fallback_ms = 0
            if parsed["caption"] and (not parsed["hashtags"] or not parsed["alt_text"]):
                fb_prompt = HASHTAG_ALT_PROMPT.format(
                    caption=parsed["caption"][:1200],
                    axes=short_axes(camp.get("axis_grover_marked", [])),
                )
                t1 = time.time()
                fb_text = await call_infer(nc, fb_prompt, max_tokens=200)
                fallback_ms = int((time.time() - t1) * 1000)
                fb_parsed = parse_sections("CAPTION:\n" + parsed["caption"] + "\n" + fb_text)
                if fb_parsed["hashtags"]:
                    parsed["hashtags"] = fb_parsed["hashtags"]
                if fb_parsed["alt_text"]:
                    parsed["alt_text"] = fb_parsed["alt_text"]
            drafts.append({
                "id": cid,
                "target": camp.get("target_name", ""),
                "axes": camp.get("axis_grover_marked", []),
                "subject": camp.get("subject", ""),
                "source_link": f"https://tenet-5.github.io/quantum-accountability.html#{cid}",
                **parsed,
                "drafted_at_utc": int(time.time()),
                "infer_latency_ms": dt_ms,
                "fallback_latency_ms": fallback_ms,
                "model": "nemotron-9b",
                "transport": "mercury.infer -> llama-server:8082",
                "raw_excerpt": text[:2500],
                "status": "draft_ready",
            })
    finally:
        await nc.drain()

    out_data = {
        "generated_at_utc": int(time.time()),
        "seed": 118400,
        "source": "liril_instagram_draft_assist",
        "note": (
            "LIRIL-drafted Instagram captions. User manually copies each "
            "caption + hashtags + alt text into the Instagram app on their "
            "phone. No automation, no credentials, no API calls touch any "
            "social platform. See feedback_no_lirilclaw.md."
        ),
        "policy": {
            "automation_allowed": False,
            "credentials_in_system": False,
            "manual_posting_only": True,
            "drafter": "LIRIL via mercury.infer",
            "transport": "nats://127.0.0.1:4223 -> llama-server:8082 (RTX 5070 Ti)",
        },
        "drafts": drafts,
    }
    OUT.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {len(drafts)} drafts to {OUT}")


if __name__ == "__main__":
    asyncio.run(draft_all())
