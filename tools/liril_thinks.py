# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:30:00Z
"""liril_thinks — LIRIL's thought loop.

Subscribes to the see.* and hear.* event buses, maintains a continuous
thought-stream (data/liril_thoughts.jsonl), and decides when a signal
is worth proposing a backlog task for the dev team.

This is CPU-only and runs during gaming. When a signal would benefit
from GPU inference (e.g. LIRIL-drafted task title), it enqueues a
deferred inference request that fires when mercury.infer is reachable.

Heuristic thought-generation rules (v1):
  file_change on data/*.json           → note 'observation' thought
  new_commit (non-autonomous, jargon)  → note 'alert' thought; propose WS for revert review
  new_commit (autonomous) verdict PASS → note 'good' thought
  transcript with verdict=FAIL         → note 'correction_needed' thought
  hear.news  containing axis keywords  → note 'signal' thought; enqueue task proposal
  hear.parl  committee agenda change   → note 'signal' thought
  hear.court new Federal Court ruling  → note 'signal' thought; potential Layer-5 chronology update

Published subjects:
  tenet5.liril.think.thought        — every thought written
  tenet5.liril.think.propose_task   — a proposed backlog task (for PM review)

Writes a rolling buffer to data/liril_thoughts.jsonl (capped at ~10k
entries via tail-truncate) so the public liril-thinks.html page can
render the stream.
"""
from __future__ import annotations
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
import time
from pathlib import Path

os.environ.setdefault("NATS_URL", "nats://127.0.0.1:4223")
import nats  # type: ignore

SITE = Path(r"E:\TENET-5.github.io")
THOUGHTS = SITE / "data" / "liril_thoughts.jsonl"
PROPOSALS = SITE / "data" / "liril_task_proposals.jsonl"

# Canadian-accountability axis keywords. When a news/parl/court item
# contains any of these, the thinking loop escalates to 'signal' severity.
AXIS_KEYWORDS = [
    # Officers of Parliament
    "ethics commissioner", "auditor general", "parliamentary budget officer",
    "commissioner of lobbying", "privacy commissioner", "pbo ", "oag ",
    # Key files / cases
    "arrivecan", "snc-lavalin", "we charity", "phoenix pay",
    "canadian surface combatant", "csc program", "mckinsey",
    "emergencies act", "foreign interference", "hogue commission",
    "rouleau commission", "arbour report",
    # Named actors
    "justin trudeau", "mark carney", "chrystia freeland", "bill morneau",
    "harjit sajjan", "anita anand", "david lametti", "marco mendicino",
    "dominic leblanc", "bill blair", "jody wilson-raybould",
    "jonathan vance", "brenda lucki", "christiane fox",
    # Courts / legal
    "federal court", "supreme court of canada", "2024 fc ", "2025 fc ",
    "royal assent", "bill c-70", "bill c-62",
]

THOUGHT_TAIL_CAP = 10_000


def _log(tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] [THINKS/{tag}] {msg}", flush=True)


def append_thought(thought: dict) -> None:
    THOUGHTS.parent.mkdir(parents=True, exist_ok=True)
    with THOUGHTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(thought, ensure_ascii=False) + "\n")
    # Tail-truncate
    try:
        size = THOUGHTS.stat().st_size
        if size > 5_000_000:  # 5 MB
            lines = THOUGHTS.read_text(encoding="utf-8").splitlines()[-THOUGHT_TAIL_CAP:]
            THOUGHTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def append_proposal(proposal: dict) -> None:
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    with PROPOSALS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(proposal, ensure_ascii=False) + "\n")


def classify_news_relevance(text: str) -> list[str]:
    low = (text or "").lower()
    return [k for k in AXIS_KEYWORDS if k in low]


class Thinker:
    def __init__(self, nc):
        self.nc = nc
        self.counters = {"see_file": 0, "see_commit": 0, "see_transcript": 0,
                         "hear_news": 0, "hear_parl": 0, "hear_court": 0,
                         "thoughts": 0, "proposals": 0}

    async def on_file_change(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["see_file"] += 1
        ext = d.get("ext", "")
        path = d.get("path", "")
        # Only think about data + content changes, not every build artifact
        if ext not in (".json", ".html", ".md"):
            return
        # Skip ourselves
        if path in ("data/liril_thoughts.jsonl", "data/liril_task_proposals.jsonl",
                    "data/.liril_hears_seen.json", "data/.liril_sees_last_commit",
                    "data/liril_perception_queue.jsonl"):
            return
        await self.think({
            "kind":     "observation",
            "source":   "see.file_change",
            "path":     path,
            "change":   d.get("change"),
            "summary":  f"file {d.get('change','modified')}: {path}",
            "severity": "low",
        })

    async def on_new_commit(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["see_commit"] += 1
        subject = d.get("subject", "")
        if d.get("is_jargon"):
            await self.think({
                "kind":     "alert",
                "source":   "see.new_commit",
                "sha":      d.get("sha", "")[:10],
                "summary":  f"jargon-slipping commit landed: {subject[:80]}",
                "severity": "high",
            })
            # Propose a revert-review task
            await self.propose_task({
                "title":        f"Review jargon commit {d.get('sha','')[:10]} for revert",
                "axis_domain":  "TECHNOLOGY",
                "priority":     "high",
                "role_pipeline":["researcher","architect","engineer","editor","gatekeeper"],
                "target_files": ["data/liril_work_schedule.json"],
                "acceptance":   f"Review git diff of {d.get('sha','')[:10]}; if content is pseudo-scientific stub (emh_*, abcxyz_*, millennial_falcon_*), add to needs_revert list.",
                "context":      f"Subject: {subject}",
                "auto_proposed": True,
            })
        elif subject.startswith("autonomous("):
            await self.think({
                "kind":     "good",
                "source":   "see.new_commit",
                "sha":      d.get("sha", "")[:10],
                "summary":  f"autonomous commit landed: {subject[:80]}",
                "severity": "low",
            })
        else:
            await self.think({
                "kind":     "observation",
                "source":   "see.new_commit",
                "sha":      d.get("sha", "")[:10],
                "summary":  f"new commit by {d.get('author','?')}: {subject[:80]}",
                "severity": "low",
            })

    async def on_transcript(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["see_transcript"] += 1
        verdict = d.get("verdict")
        status = d.get("status")
        task = d.get("task_id")
        if verdict == "FAIL" or status == "schema_validation_failed":
            await self.think({
                "kind":     "correction_needed",
                "source":   "see.transcript",
                "path":     d.get("path"),
                "task_id":  task,
                "summary":  f"cycle {task} failed ({status or verdict}) — transcript ready for correction logging",
                "severity": "high",
            })
        elif verdict == "WATCH":
            await self.think({
                "kind":     "watch",
                "source":   "see.transcript",
                "path":     d.get("path"),
                "task_id":  task,
                "summary":  f"cycle {task} WATCH — firmer source anchoring wanted",
                "severity": "medium",
            })
        elif verdict == "PASS":
            await self.think({
                "kind":     "good",
                "source":   "see.transcript",
                "path":     d.get("path"),
                "task_id":  task,
                "summary":  f"cycle {task} PASS",
                "severity": "low",
            })

    async def on_news(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["hear_news"] += 1
        combined = d.get("title","") + " " + d.get("summary","")
        hits = classify_news_relevance(combined)
        if not hits:
            return  # irrelevant news, ignore
        await self.think({
            "kind":     "signal",
            "source":   "hear.news",
            "link":     d.get("link"),
            "title":    d.get("title"),
            "keywords": hits,
            "summary":  f"news item matches {len(hits)} axis keyword(s): {d.get('title','')[:100]}",
            "severity": "high" if len(hits) >= 3 else "medium",
        })
        # Propose a task when 3+ axis keywords match
        if len(hits) >= 3:
            await self.propose_task({
                "title":        f"Evaluate news item for Layer-5 chronology: {d.get('title','')[:60]}",
                "axis_domain":  "ETHICS",
                "priority":     "medium",
                "role_pipeline":["researcher","architect","editor","gatekeeper"],
                "target_files": ["data/enforcement_followthrough.json"],
                "acceptance":   f"If the news item at {d.get('link')} describes an event connected to an existing Layer-5 finding, add a chronology entry with type, date, detail, source_url.",
                "context":      f"Matched keywords: {', '.join(hits)}. Title: {d.get('title','')}.",
                "auto_proposed": True,
            })

    async def on_parl(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["hear_parl"] += 1
        combined = d.get("title","") + " " + d.get("summary","")
        hits = classify_news_relevance(combined)
        if not hits:
            return
        await self.think({
            "kind":     "signal",
            "source":   "hear.parl",
            "link":     d.get("link"),
            "title":    d.get("title"),
            "keywords": hits,
            "summary":  f"parliamentary item matches {len(hits)} keyword(s): {d.get('title','')[:100]}",
            "severity": "medium",
        })

    async def on_court(self, msg):
        d = json.loads(msg.data.decode("utf-8", errors="replace"))
        self.counters["hear_court"] += 1
        await self.think({
            "kind":     "signal",
            "source":   "hear.court",
            "link":     d.get("link"),
            "title":    d.get("title"),
            "summary":  f"court docket movement: {d.get('title','')[:100]}",
            "severity": "high",
        })

    async def think(self, thought_fields: dict) -> None:
        thought = {"ts": int(time.time()), **thought_fields}
        append_thought(thought)
        await self.nc.publish("tenet5.liril.think.thought", json.dumps(thought).encode("utf-8"))
        self.counters["thoughts"] += 1
        if thought.get("severity") == "high":
            _log("THINK", thought["summary"][:120])

    async def propose_task(self, task_fields: dict) -> None:
        proposal = {"proposed_at": int(time.time()), **task_fields}
        append_proposal(proposal)
        await self.nc.publish("tenet5.liril.think.propose_task", json.dumps(proposal).encode("utf-8"))
        self.counters["proposals"] += 1
        _log("PROPOSE", task_fields.get("title", "")[:120])

    async def heartbeat(self):
        while True:
            await asyncio.sleep(60)
            _log("HEARTBEAT", str(self.counters))


async def main() -> None:
    _log("BOOT", f"NATS_URL={os.environ['NATS_URL']}")
    while True:
        try:
            nc = await nats.connect(os.environ["NATS_URL"], connect_timeout=5,
                                     reconnect_time_wait=2, max_reconnect_attempts=-1)
            t = Thinker(nc)
            await nc.subscribe("tenet5.liril.see.file_change",  cb=t.on_file_change)
            await nc.subscribe("tenet5.liril.see.new_commit",   cb=t.on_new_commit)
            await nc.subscribe("tenet5.liril.see.transcript",   cb=t.on_transcript)
            await nc.subscribe("tenet5.liril.hear.news",        cb=t.on_news)
            await nc.subscribe("tenet5.liril.hear.parl",        cb=t.on_parl)
            await nc.subscribe("tenet5.liril.hear.court",       cb=t.on_court)
            _log("SUBS", "subscribed to 6 subjects")
            await t.heartbeat()
        except Exception as e:
            _log("ERROR", f"top-level {e!r}; sleep 5 and reconnect")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
