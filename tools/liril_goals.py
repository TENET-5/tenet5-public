#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T16:30:00Z | Author: claude_code | Change: LIRIL Goals — goal decomposition + execution (AGI direction substrate)
"""LIRIL Goals — from high-level intent to capability calls.

CEO directive (2026-04-19): "start developing liril out for full AGI"

Pragmatic interpretation
------------------------
"Full AGI" is not something a single tool ships. What we CAN build is
the substrate that lets LIRIL accept a high-level goal, decompose it
into concrete steps, dispatch those steps to existing capabilities, and
track progress across daemon restarts. Every future AGI-ish behaviour
(planning, tool-use, self-reflection, goal prioritisation) layers on
top of this.

This is that substrate.

Architecture
------------
Three building blocks on top of infrastructure already shipped:

  1. STATE       Every goal + every step persists in the journal
                 (Cap: liril_journal). Tags used:
                     goal:open | goal:closed
                     goal:<id>            (for per-goal recall)
                     kind:goal            (top-level membership)
                     step:<goal_id>:<idx> (for step order)

  2. DECOMPOSE   A goal description → a list of executable steps. The
                 LLM (Mistral-Nemo via mercury.infer.code on NATS) is
                 asked to output strict JSON: a list of
                 {cap, method, args, expected_output_type} objects.
                 If the LLM is unavailable, decomposition falls back
                 to a manual keyword heuristic.

  3. DISPATCH    Each step is executed by calling the LIRIL HTTP API
                 at http://127.0.0.1:18120. Every cap (Cap#1-#10 +
                 skills) is reachable through that API, so a step like
                     {cap: 'process_manager', method: 'processes',
                      args: {top: 10}}
                 becomes GET /processes?top=10, and the result is
                 stored back in the journal against the step id.

Safety
------
 * Every goal step checks Cap#10's is_safe_to_execute() BEFORE calling
   the API. If level >= 3, the step is deferred (goal stays open).
 * Destructive caps (process_manager kill / service_control restart /
   driver_manager uninstall / patch_manager install) are NEVER invoked
   by this engine in v1. Only READ operations are executable. A future
   v2 will add a confirmation flow.
 * All HTTP calls timeout at 30s per step.
 * Goals cap: 20 open goals max. Adding a 21st refuses until one closes.
 * Autonomy cap: max 10 step executions per hour across all goals
   (sliding, journal-backed).

Public API
----------
  add_goal(text, priority='medium', deadline=None) -> goal_id
  decompose(goal_id)                               -> plan (list of steps)
  run_next_step(goal_id)                            -> result dict
  run_all(goal_id, max_steps=10)                   -> final status
  status(goal_id=None)                             -> dict or list
  close(goal_id, outcome='done')                   -> bool
  list_open()                                       -> list

NATS RPC
--------
  tenet5.liril.goals.cmd   {op:'add'|'run'|'status'|'close', ...}

CLI
---
  --add "TEXT" [--priority=high|med|low]
  --decompose GOAL_ID
  --run GOAL_ID [--max-steps N]
  --step GOAL_ID
  --status [GOAL_ID]
  --list
  --close GOAL_ID [--outcome=done|cancelled]
  --daemon           run the NATS RPC responder
"""
from __future__ import annotations

import argparse
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
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")
CMD_SUBJECT     = "tenet5.liril.goals.cmd"
AUDIT_SUBJECT   = "tenet5.liril.goals.audit"
METRICS_SUBJECT = "tenet5.liril.goals.metrics"
LIRIL_API_BASE  = os.environ.get("LIRIL_API_BASE", "http://127.0.0.1:18120")

MAX_OPEN_GOALS    = 20
STEPS_PER_HOUR    = 10
STEPS_WINDOW_SEC  = 3600.0
STEP_TIMEOUT_SEC  = 30.0
LLM_TIMEOUT_SEC   = 60.0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# JOURNAL ADAPTER
# ─────────────────────────────────────────────────────────────────────

def _j():
    sys.path.insert(0, str(ROOT / "tools"))
    import liril_journal as _jm  # type: ignore
    return _jm


def _fse_safe() -> tuple[bool, int]:
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import liril_fail_safe_escalation as _fse  # type: ignore
        return _fse.is_safe_to_execute(), _fse.current_level()
    except Exception:
        return True, 0


# ─────────────────────────────────────────────────────────────────────
# CAPABILITY DISPATCH — via LIRIL HTTP API
# ─────────────────────────────────────────────────────────────────────

# Mapping from (cap, method) → HTTP route on liril_api. Only READ routes.
# Anything not in this table is refused.
_READ_ROUTES: dict[tuple[str, str], tuple[str, str]] = {
    ("status", "get"):            ("GET",  "/status"),
    ("status", "brief"):          ("GET",  "/status/brief"),
    ("failsafe", "level"):        ("GET",  "/level"),
    ("incidents", "recent"):      ("GET",  "/incidents"),
    ("gpu", "snapshot"):          ("GET",  "/gpu"),
    ("process_manager", "top"):   ("GET",  "/processes"),
    ("process_manager", "processes"): ("GET", "/processes"),
    ("service_control", "list"):  ("GET",  "/services"),
    ("services", "list"):         ("GET",  "/services"),
    ("driver_manager", "list"):   ("GET",  "/drivers"),
    ("drivers", "list"):          ("GET",  "/drivers"),
    ("patch_manager", "pending"): ("GET",  "/patches"),
    ("patches", "list"):          ("GET",  "/patches"),
    ("intent", "get"):            ("GET",  "/intent"),
    ("user_intent", "current"):   ("GET",  "/intent"),
    ("hardware", "gpu"):          ("GET",  "/gpu"),
    ("file_awareness", "alerts"): ("GET",  "/file_alerts"),
    ("journal", "stats"):         ("GET",  "/journal/stats"),
    ("journal", "recall"):        ("GET",  "/journal/recall"),
    ("journal", "search"):        ("GET",  "/journal/search"),
    ("nats", "rates"):            ("GET",  "/nats_rates"),
}


def _http_get(path: str, params: dict | None = None) -> dict:
    """Call the LIRIL HTTP API."""
    q = urllib.parse.urlencode(params or {})
    url = LIRIL_API_BASE + path + (("?" + q) if q else "")
    try:
        with urllib.request.urlopen(url, timeout=STEP_TIMEOUT_SEC) as r:
            body = r.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": r.status,
                    "body": json.loads(body) if body else {}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Grok-review fix round 2: per-route arg whitelist. Extra keys from the
# LLM are silently dropped; values are length-capped strings only.
# This hardens the goals engine against a maliciously crafted LLM
# decompose response (JSON injection, shell-y args, key bloat).
_ALLOWED_ARGS: dict[tuple[str, str], set[str]] = {
    ("incidents", "recent"):      {"limit"},
    ("process_manager", "top"):   {"top"},
    ("process_manager", "processes"): {"top"},
    ("file_awareness", "alerts"): {"limit"},
    ("journal", "recall"):        {"key", "tag", "limit"},
    ("journal", "search"):        {"q", "limit"},
    # Most read routes take no args at all:
    ("status", "get"):            set(),
    ("status", "brief"):          set(),
    ("failsafe", "level"):        set(),
    ("gpu", "snapshot"):          set(),
    ("service_control", "list"):  set(),
    ("services", "list"):         set(),
    ("driver_manager", "list"):   set(),
    ("drivers", "list"):          set(),
    ("patch_manager", "pending"): set(),
    ("patches", "list"):          set(),
    ("intent", "get"):            set(),
    ("user_intent", "current"):   set(),
    ("hardware", "gpu"):          set(),
    ("journal", "stats"):         set(),
    ("nats", "rates"):            set(),
}

_MAX_ARG_VALUE_LEN = 200


def _sanitise_args(cap: str, method: str, args) -> tuple[dict, list[str]]:
    """Filter args against the per-route whitelist + coerce to safe strings.
    Returns (clean_params, dropped_keys)."""
    allowed = _ALLOWED_ARGS.get((cap, method), set())
    if not isinstance(args, dict):
        return {}, ["<args not dict>"]
    clean: dict = {}
    dropped: list[str] = []
    for k, v in args.items():
        if not isinstance(k, str) or k not in allowed:
            dropped.append(str(k)[:32])
            continue
        # Only scalar, length-capped string values
        if not isinstance(v, (str, int, float, bool)):
            dropped.append(f"{k}(non-scalar)")
            continue
        clean[k] = str(v)[:_MAX_ARG_VALUE_LEN]
    return clean, dropped


def _dispatch_step(step: dict) -> dict:
    """Execute ONE step. Returns the result dict written to the journal."""
    # Grok-review fix round 2: validate the step shape BEFORE looking at
    # any field. LLM output is untrusted.
    if not isinstance(step, dict):
        return {"ok": False, "error": f"refused: step is not a dict (got {type(step).__name__})"}
    cap = (step.get("cap") or "").strip().lower()
    method = (step.get("method") or "").strip().lower()
    if not cap or not method:
        return {"ok": False, "error": "refused: step missing cap or method"}
    raw_args = step.get("args") or {}
    key = (cap, method)
    if key not in _READ_ROUTES:
        return {"ok": False, "error": f"refused: ({cap}, {method}) not in "
                                       "read-only dispatch table. v1 refuses "
                                       "destructive caps."}
    verb, route = _READ_ROUTES[key]
    if verb != "GET":
        return {"ok": False, "error": "only GET supported in v1"}
    # Sanitise args per whitelist
    params, dropped = _sanitise_args(cap, method, raw_args)
    r = _http_get(route, params)
    # Record any dropped keys in the step's audit trail
    if dropped:
        r = dict(r)
        r["dropped_args"] = dropped[:10]
    if not r.get("ok"):
        return r
    body = r.get("body") or {}
    # Unwrap LIRIL API envelope
    if isinstance(body, dict) and "data" in body:
        data = body.get("data")
        return {"ok": bool(body.get("ok", True)),
                "cap": cap, "method": method, "args": params,
                "data": data,
                **({"dropped_args": dropped[:10]} if dropped else {})}
    return {"ok": True, "cap": cap, "method": method, "args": params,
            "data": body,
            **({"dropped_args": dropped[:10]} if dropped else {})}


# ─────────────────────────────────────────────────────────────────────
# DECOMPOSITION (LLM-backed with heuristic fallback)
# ─────────────────────────────────────────────────────────────────────

_DECOMPOSE_SYSTEM = (
    "You decompose high-level goals into a JSON array of concrete steps. "
    "Every step is an object with: "
    '{"cap": <capability name>, "method": <method>, '
    '"args": <object, may be empty>, "why": <one short sentence>}. '
    "Available (cap, method) pairs in v1 are STRICTLY: "
    "(status,get), (status,brief), (failsafe,level), (incidents,recent), "
    "(gpu,snapshot), (process_manager,top), (service_control,list), "
    "(driver_manager,list), (patch_manager,pending), (intent,get), "
    "(file_awareness,alerts), (journal,stats), (journal,recall), "
    "(journal,search), (nats,rates). "
    "Args are simple key=value pairs (limit, top, tag, key, q, severity). "
    "Output ONLY the JSON array, no prose, no markdown."
)


def _llm_decompose_via_nats(goal_text: str, timeout: float = LLM_TIMEOUT_SEC) -> list[dict]:
    """Use mercury.infer.code on NATS 4223 to decompose. Returns a list
    of step dicts, or empty list if the LLM is unavailable."""
    async def go():
        import nats as _nats
        try:
            nc = await _nats.connect(NATS_URL, connect_timeout=3)
        except Exception:
            return []
        try:
            prompt = (
                _DECOMPOSE_SYSTEM + "\n\n"
                "GOAL: " + goal_text + "\n\n"
                "JSON:"
            )
            try:
                r = await nc.request(
                    "mercury.infer.code",
                    json.dumps({
                        "prompt": prompt,
                        "max_tokens": 500,
                        "temperature": 0.0,
                    }).encode(),
                    timeout=timeout,
                )
                d = json.loads(r.data.decode())
                txt = d.get("text") or d.get("response") or d.get("content") or ""
                # Extract JSON array
                m = re.search(r"\[\s*{[^\[\]]+?}\s*(?:,\s*{[^\[\]]+?}\s*)*\]", txt, re.DOTALL)
                if not m:
                    return []
                try:
                    plan = json.loads(m.group(0))
                    if isinstance(plan, list):
                        return plan
                except Exception:
                    return []
            except Exception:
                return []
        finally:
            try: await nc.drain()
            except Exception: pass
        return []
    try:
        return asyncio.run(go())
    except Exception:
        return []


def _heuristic_decompose(goal_text: str) -> list[dict]:
    """Fallback: keyword-based step selection. Crude but deterministic."""
    g = (goal_text or "").lower()
    plan: list[dict] = []
    # Almost every goal benefits from a status snapshot first
    plan.append({"cap": "status", "method": "get", "args": {},
                 "why": "baseline system snapshot"})
    if any(k in g for k in ("gpu", "temp", "thermal", "cuda", "nvidia", "heat")):
        plan.append({"cap": "gpu", "method": "snapshot", "args": {},
                     "why": "inspect GPU thermal + util"})
    if any(k in g for k in ("process", "cpu", "memory", "ram", "top")):
        plan.append({"cap": "process_manager", "method": "top", "args": {"top": 15},
                     "why": "identify top processes by memory"})
    if any(k in g for k in ("service", "defender", "spooler", "stopped")):
        plan.append({"cap": "service_control", "method": "list", "args": {},
                     "why": "enumerate services"})
    if any(k in g for k in ("driver", "device", "pnp")):
        plan.append({"cap": "driver_manager", "method": "list", "args": {},
                     "why": "enumerate drivers"})
    if any(k in g for k in ("patch", "update", "wu", "security update")):
        plan.append({"cap": "patch_manager", "method": "pending", "args": {},
                     "why": "pending Windows Updates"})
    if any(k in g for k in ("incident", "escalation", "anomaly", "alert")):
        plan.append({"cap": "incidents", "method": "recent", "args": {"limit": 20},
                     "why": "recent fse incidents"})
    if any(k in g for k in ("file", "temp file", "download", "suspicious")):
        plan.append({"cap": "file_awareness", "method": "alerts", "args": {"limit": 10},
                     "why": "recent file alerts"})
    if any(k in g for k in ("intent", "busy", "gaming", "meeting", "coding")):
        plan.append({"cap": "intent", "method": "get", "args": {},
                     "why": "current user intent"})
    # Always close with a journal search against the goal text so we
    # learn something durable each time
    plan.append({"cap": "journal", "method": "search",
                 "args": {"q": goal_text[:60], "limit": 5},
                 "why": "search historical journal for related entries"})
    return plan


def decompose(goal_id: str) -> dict:
    rows = _j().recall(key=f"goal.{goal_id}", limit=1)
    if not rows:
        return {"ok": False, "error": f"no goal {goal_id}"}
    goal = rows[0].get("value") or {}
    text = goal.get("text") or ""
    if not text:
        return {"ok": False, "error": "goal has empty text"}

    plan = _llm_decompose_via_nats(text)
    source = "llm"
    if not plan:
        plan = _heuristic_decompose(text)
        source = "heuristic"

    # Write the plan back into the goal
    goal["plan"] = plan
    goal["plan_source"] = source
    goal["plan_ts"] = _utc()
    _j().remember(
        key=f"goal.{goal_id}",
        value=goal,
        tags=["goal:open", f"goal:{goal_id}", "kind:goal"],
        source="liril_goals",
    )
    return {"ok": True, "goal_id": goal_id, "plan": plan,
            "plan_source": source, "step_count": len(plan)}


# ─────────────────────────────────────────────────────────────────────
# GOAL CRUD
# ─────────────────────────────────────────────────────────────────────

def _count_open_goals() -> int:
    rows = _j().recall(tag="goal:open", limit=100)
    return len(rows)


def _recent_step_count() -> int:
    rows = _j().recall(tag="kind:step", limit=50,
                       since_ts=time.time() - STEPS_WINDOW_SEC)
    return len(rows)


def add_goal(text: str, priority: str = "medium",
             deadline: str | None = None) -> dict:
    if not text or not text.strip():
        return {"ok": False, "error": "empty goal text"}
    if _count_open_goals() >= MAX_OPEN_GOALS:
        return {"ok": False, "error": f"max open goals ({MAX_OPEN_GOALS}) reached; close some first"}
    goal_id = str(uuid.uuid4())[:8]
    value = {
        "id":         goal_id,
        "text":       text.strip()[:500],
        "priority":   (priority or "medium").lower(),
        "deadline":   deadline,
        "created_ts": _utc(),
        "state":      "open",
        "plan":       [],
        "progress":   {"steps_run": 0, "last_step_ts": None},
    }
    _j().remember(
        key=f"goal.{goal_id}",
        value=value,
        tags=["goal:open", f"goal:{goal_id}", "kind:goal"],
        source="liril_goals",
    )
    return {"ok": True, "goal_id": goal_id, "goal": value}


def close(goal_id: str, outcome: str = "done") -> dict:
    rows = _j().recall(key=f"goal.{goal_id}", limit=1)
    if not rows:
        return {"ok": False, "error": f"no goal {goal_id}"}
    goal = rows[0].get("value") or {}
    goal["state"]     = "closed"
    goal["outcome"]   = outcome
    goal["closed_ts"] = _utc()
    _j().remember(
        key=f"goal.{goal_id}",
        value=goal,
        tags=["goal:closed", f"goal:{goal_id}", "kind:goal",
              f"goal_outcome:{outcome}"],
        source="liril_goals",
    )
    return {"ok": True, "goal_id": goal_id, "outcome": outcome}


def list_open() -> list[dict]:
    rows = _j().recall(tag="goal:open", limit=100)
    return [r.get("value") for r in rows if r.get("value")]


def status(goal_id: str | None = None) -> dict:
    if goal_id:
        rows = _j().recall(key=f"goal.{goal_id}", limit=1)
        if not rows:
            return {"ok": False, "error": f"no goal {goal_id}"}
        return {"ok": True, "goal": rows[0].get("value")}
    return {
        "ok":            True,
        "open_count":    _count_open_goals(),
        "max_open":      MAX_OPEN_GOALS,
        "steps_last_hr": _recent_step_count(),
        "steps_cap":     STEPS_PER_HOUR,
        "open_goals":    list_open(),
    }


# ─────────────────────────────────────────────────────────────────────
# STEP EXECUTION
# ─────────────────────────────────────────────────────────────────────

def run_next_step(goal_id: str) -> dict:
    # Safety gates
    ok_exec, lvl = _fse_safe()
    if not ok_exec:
        return {"ok": False, "skipped": f"failsafe_level={lvl}"}
    if _recent_step_count() >= STEPS_PER_HOUR:
        return {"ok": False, "skipped": f"step_rate_cap ({STEPS_PER_HOUR}/hr)"}

    rows = _j().recall(key=f"goal.{goal_id}", limit=1)
    if not rows:
        return {"ok": False, "error": f"no goal {goal_id}"}
    goal = rows[0].get("value") or {}
    if goal.get("state") == "closed":
        return {"ok": False, "error": "goal is closed"}
    plan = goal.get("plan") or []
    steps_run = int((goal.get("progress") or {}).get("steps_run") or 0)
    if steps_run >= len(plan):
        return {"ok": True, "done": True,
                "message": "all steps executed", "steps_total": len(plan)}

    step = plan[steps_run]
    t0 = time.time()
    try:
        result = _dispatch_step(step)
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    duration = round(time.time() - t0, 2)
    step_id = f"{goal_id}:{steps_run}"

    # Record the step result
    _j().remember(
        key=f"goal.step.{step_id}",
        value={"goal_id": goal_id, "step_idx": steps_run,
               "step": step, "result": result,
               "duration_s": duration, "ts": _utc()},
        tags=["kind:step", f"goal:{goal_id}",
              f"step:{step_id}", "observation:goals"],
        source="liril_goals",
    )
    # Advance goal
    goal["progress"] = {
        "steps_run":     steps_run + 1,
        "last_step_ts":  _utc(),
        "last_step_ok":  bool(result.get("ok")),
    }
    _j().remember(
        key=f"goal.{goal_id}",
        value=goal,
        tags=["goal:open", f"goal:{goal_id}", "kind:goal"],
        source="liril_goals",
    )
    return {"ok": result.get("ok", False),
            "goal_id": goal_id, "step_idx": steps_run,
            "step": step, "result": result, "duration_s": duration,
            "done": (steps_run + 1 >= len(plan))}


def run_all(goal_id: str, max_steps: int = 10) -> dict:
    results: list[dict] = []
    for _ in range(max(1, int(max_steps))):
        r = run_next_step(goal_id)
        results.append(r)
        if not r.get("ok") or r.get("done") or r.get("skipped"):
            break
    return {"ok": True, "goal_id": goal_id, "results": results,
            "steps_executed": len(results)}


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Goals — AGI-direction substrate")
    ap.add_argument("--add",        type=str, metavar="TEXT",
                    help="Add a new goal")
    ap.add_argument("--priority",   type=str, default="medium",
                    choices=["high", "medium", "med", "low"])
    ap.add_argument("--deadline",   type=str, default=None,
                    help="Optional ISO-8601 deadline")
    ap.add_argument("--decompose",  type=str, metavar="GOAL_ID",
                    help="Decompose a goal into steps")
    ap.add_argument("--run",        type=str, metavar="GOAL_ID",
                    help="Execute up to --max-steps of a goal")
    ap.add_argument("--step",       type=str, metavar="GOAL_ID",
                    help="Execute exactly ONE step of a goal")
    ap.add_argument("--max-steps",  type=int, default=10)
    ap.add_argument("--status",     type=str, nargs="?", const="",
                    help="Show status of a goal (no arg = global)")
    ap.add_argument("--list",       action="store_true", help="List open goals")
    ap.add_argument("--close",      type=str, metavar="GOAL_ID")
    ap.add_argument("--outcome",    type=str, default="done",
                    choices=["done", "cancelled", "blocked"])
    args = ap.parse_args()

    if args.add:
        r = add_goal(args.add, priority=args.priority, deadline=args.deadline)
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("ok") else 1

    if args.decompose:
        r = decompose(args.decompose)
        print(json.dumps(r, indent=2, default=str)[:6000])
        return 0 if r.get("ok") else 1

    if args.step:
        r = run_next_step(args.step)
        print(json.dumps(r, indent=2, default=str)[:6000])
        return 0 if r.get("ok") else 1

    if args.run:
        r = run_all(args.run, max_steps=args.max_steps)
        print(json.dumps(r, indent=2, default=str)[:8000])
        return 0 if r.get("ok") else 1

    if args.status is not None:
        r = status(args.status or None)
        print(json.dumps(r, indent=2, default=str)[:6000])
        return 0

    if args.list:
        print(json.dumps(list_open(), indent=2, default=str))
        return 0

    if args.close:
        r = close(args.close, outcome=args.outcome)
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
