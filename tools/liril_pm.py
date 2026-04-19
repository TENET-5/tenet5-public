# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T03:55:00Z
"""liril_pm — the Project Manager bridge.

Subscribes to tenet5.liril.think.propose_task. When liril_thinks
proposes a task, this daemon:
  1. Validates the proposal against a minimal schema.
  2. De-duplicates against the existing backlog by title similarity.
  3. Assigns the next WS-NNN id.
  4. Appends to data/liril_work_schedule.json backlog.
  5. Publishes tenet5.liril.pm.accepted or tenet5.liril.pm.rejected.
  6. Writes a thought to data/liril_thoughts.jsonl so the live page
     shows the promotion happen in real time.

This closes the think→do gap: LIRIL can now add work to its OWN
backlog, which the liril_dev_team daemon then picks up on its next
cycle. Self-directing autonomy.

Safety rails:
  - Target files in the proposal must all be in ALLOWED_TARGETS
    (conservative whitelist — data/*.json only at first).
  - Title ≤ 120 chars; acceptance ≤ 500 chars.
  - Max 10 new tasks per hour (rate limit).
  - De-dup by lowercased alphanumeric title fragment.

CPU-only, no GPU, safe to run during gaming.
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
import re
import time
from pathlib import Path

os.environ.setdefault("NATS_URL", "nats://127.0.0.1:4223")
import nats  # type: ignore

SITE = Path(r"E:\TENET-5.github.io")
BOARD = SITE / "data" / "liril_work_schedule.json"
THOUGHTS = SITE / "data" / "liril_thoughts.jsonl"
PM_LOG = SITE / "data" / "liril_pm_decisions.jsonl"
GAME_MODE = Path(r"E:\S.L.A.T.E\tenet5\data\.game_mode")

ALLOWED_TARGETS_PATTERNS = [
    re.compile(r"^data/[\w/_-]+\.json$"),
    re.compile(r"^data/[\w/_-]+\.jsonl$"),
    re.compile(r"^docs/[\w/_-]+\.md$"),
]

MAX_TITLE_LEN = 120
MAX_ACCEPTANCE_LEN = 500
MAX_PROMOTIONS_PER_HOUR = 10
DUP_SIMILARITY_THRESHOLD = 0.72  # Jaccard on alphanumeric tokens


def _log(tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] [PM/{tag}] {msg}", flush=True)


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) >= 3}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_board() -> dict:
    return json.loads(BOARD.read_text(encoding="utf-8"))


def save_board(board: dict) -> None:
    board["updated_at"] = int(time.time())
    BOARD.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


def next_id(board: dict) -> str:
    nums = []
    for t in board.get("backlog", []) + board.get("done_today", []) + board.get("in_progress", []):
        m = re.match(r"^WS-(\d+)$", t.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"WS-{n:03d}"


def validate(proposal: dict) -> tuple[bool, str]:
    title = (proposal.get("title") or "").strip()
    acc = (proposal.get("acceptance") or "").strip()
    targets = proposal.get("target_files") or []

    if not title:
        return False, "missing title"
    if len(title) > MAX_TITLE_LEN:
        return False, f"title > {MAX_TITLE_LEN} chars"
    if not acc:
        return False, "missing acceptance"
    if len(acc) > MAX_ACCEPTANCE_LEN:
        return False, f"acceptance > {MAX_ACCEPTANCE_LEN} chars"
    if not targets:
        return False, "no target_files"
    for tf in targets:
        if not any(p.match(tf) for p in ALLOWED_TARGETS_PATTERNS):
            return False, f"target {tf!r} not in ALLOWED_TARGETS_PATTERNS"
    return True, ""


def find_duplicate(board: dict, title: str) -> str | None:
    """Return id of existing task with Jaccard similarity >= threshold."""
    tgt = _tokens(title)
    if not tgt:
        return None
    all_tasks = board.get("backlog", []) + board.get("done_today", []) + board.get("in_progress", [])
    best_id = None
    best_score = 0.0
    for t in all_tasks:
        existing = _tokens(t.get("title", ""))
        score = jaccard(tgt, existing)
        if score > best_score:
            best_score = score
            best_id = t.get("id")
    if best_score >= DUP_SIMILARITY_THRESHOLD:
        return best_id
    return None


class RateLimiter:
    def __init__(self, max_per_hour: int):
        self.max = max_per_hour
        self.window: list[float] = []

    def allow(self) -> bool:
        now = time.time()
        self.window = [t for t in self.window if now - t < 3600]
        if len(self.window) >= self.max:
            return False
        self.window.append(now)
        return True


async def publish_thought(nc, kind: str, summary: str, severity: str = "medium",
                          extra: dict | None = None) -> None:
    thought = {
        "ts":       int(time.time()),
        "kind":     kind,
        "source":   "pm.decision",
        "summary":  summary,
        "severity": severity,
    }
    if extra:
        thought.update(extra)
    try:
        with THOUGHTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(thought, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        await nc.publish("tenet5.liril.think.thought", json.dumps(thought).encode("utf-8"))
    except Exception:
        pass


async def write_pm_log(decision: dict) -> None:
    try:
        with PM_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision, ensure_ascii=False) + "\n")
    except Exception:
        pass


class PM:
    def __init__(self, nc):
        self.nc = nc
        self.rate = RateLimiter(MAX_PROMOTIONS_PER_HOUR)
        self.stats = {"received": 0, "accepted": 0, "rejected_dup": 0,
                      "rejected_invalid": 0, "rate_limited": 0, "game_mode_skipped": 0}

    async def on_proposal(self, msg):
        self.stats["received"] += 1
        if GAME_MODE.exists():
            self.stats["game_mode_skipped"] += 1
            return
        try:
            proposal = json.loads(msg.data.decode("utf-8", errors="replace"))
        except Exception:
            self.stats["rejected_invalid"] += 1
            return

        ok, why = validate(proposal)
        if not ok:
            self.stats["rejected_invalid"] += 1
            decision = {"ts": int(time.time()), "action": "reject",
                        "reason": f"invalid: {why}", "proposal": proposal}
            await write_pm_log(decision)
            await self.nc.publish("tenet5.liril.pm.rejected",
                                  json.dumps(decision).encode("utf-8"))
            return

        board = load_board()
        dup = find_duplicate(board, proposal.get("title", ""))
        if dup:
            self.stats["rejected_dup"] += 1
            decision = {"ts": int(time.time()), "action": "reject",
                        "reason": f"duplicate of {dup}", "proposal": proposal}
            await write_pm_log(decision)
            await self.nc.publish("tenet5.liril.pm.rejected",
                                  json.dumps(decision).encode("utf-8"))
            return

        if not self.rate.allow():
            self.stats["rate_limited"] += 1
            decision = {"ts": int(time.time()), "action": "defer",
                        "reason": f"rate limit ({MAX_PROMOTIONS_PER_HOUR}/hour exceeded)",
                        "proposal": proposal}
            await write_pm_log(decision)
            return

        new_id = next_id(board)
        task = {
            "id":            new_id,
            "title":         proposal["title"].strip(),
            "axis_domain":   proposal.get("axis_domain", "ETHICS"),
            "priority":      proposal.get("priority", "medium"),
            "role_pipeline": proposal.get("role_pipeline",
                                          ["researcher", "architect", "engineer",
                                           "editor", "gatekeeper"]),
            "target_files":  proposal["target_files"],
            "acceptance":    proposal["acceptance"].strip(),
            "context":       proposal.get("context", "") + " [auto-proposed by LIRIL PM]",
            "status":        "pending",
            "auto_proposed": True,
            "proposed_at":   int(time.time()),
        }
        board.setdefault("backlog", []).append(task)
        stats = board.setdefault("stats", {})
        stats["total_backlog"] = len(board["backlog"]) + len(board.get("done_today", []))
        save_board(board)

        self.stats["accepted"] += 1
        decision = {"ts": int(time.time()), "action": "accept",
                    "new_task_id": new_id, "proposal": proposal, "task": task}
        await write_pm_log(decision)
        await self.nc.publish("tenet5.liril.pm.accepted",
                              json.dumps(decision).encode("utf-8"))
        await publish_thought(
            self.nc, "good",
            f"PM promoted proposal to {new_id}: {proposal['title'][:80]}",
            severity="medium",
            extra={"task_id": new_id},
        )
        _log("PROMOTE", f"{new_id}: {proposal['title'][:80]}")

    async def heartbeat(self):
        while True:
            await asyncio.sleep(120)
            _log("HEARTBEAT", str(self.stats))


async def main() -> None:
    _log("BOOT", f"NATS_URL={os.environ['NATS_URL']}")
    while True:
        try:
            nc = await nats.connect(os.environ["NATS_URL"], connect_timeout=5,
                                     reconnect_time_wait=2, max_reconnect_attempts=-1)
            pm = PM(nc)
            await nc.subscribe("tenet5.liril.think.propose_task", cb=pm.on_proposal)
            _log("SUBS", "subscribed to tenet5.liril.think.propose_task")
            await pm.heartbeat()
        except Exception as e:
            _log("ERROR", f"top-level {e!r}; sleep 5 and reconnect")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
