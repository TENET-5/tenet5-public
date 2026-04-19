# Copyright (C) 2026, TENET5 Development Team
# SPDX-License-Identifier: EOSL-2.0
# Modified: 2026-03-21T09:24:00-04:00

"""HYDROGEN Event Logger — dual-write to local JSON + LIRIL training.

Records system events to hydrogen_system_log.json and optionally
publishes to LIRIL training API for domain-aware learning.

SYSTEM_SEED=118400
"""

import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

SYSTEM_SEED = 118400
LIRIL_API = "http://127.0.0.1:18090"


def _liril_train(task: str, result: str, agent: str = "hydrogen") -> Optional[dict]:
    """Fire-and-forget training sample to LIRIL."""
    try:
        url = f"{LIRIL_API}/tools/liril_copilot_train"
        payload = json.dumps({
            "task": task[:200],
            "result": result[:200],
            "agent_id": agent,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def record_event(
    agent: str,
    user_identity: str,
    prompt_type: str,
    subsystem: str,
    event: str,
    outcome: str,
) -> None:
    """Record a system event to local log and optionally to LIRIL.

    Args:
        agent: Agent identifier (e.g., 'antigravity', 'copilot').
        user_identity: User who triggered the event.
        prompt_type: Type of prompt (e.g., 'task', 'query', 'session').
        subsystem: System component (e.g., 'nemoclaw', 'hydrogen', 'liril').
        event: Event description.
        outcome: Result of the event.
    """
    log_file = os.path.join(os.path.dirname(__file__), "..", "hydrogen_system_log.json")
    record = {
        "timestamp": time.time(),
        "agent": agent,
        "user_identity": user_identity,
        "prompt_type": prompt_type,
        "subsystem": subsystem,
        "event": event,
        "outcome": outcome,
        "seed": SYSTEM_SEED,
    }

    # ── Local JSON append ──
    logs = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            pass

    logs.append(record)
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2)

    # ── LIRIL training (fire-and-forget) ──
    task_desc = f"{subsystem}: {event}"
    result_desc = f"{outcome} (agent={agent}, prompt={prompt_type})"
    _liril_train(task_desc, result_desc, agent)

    print(f"[HYDROGEN LOG] Recorded event: {event} -> {outcome}")
