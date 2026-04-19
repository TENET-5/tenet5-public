#!/usr/bin/env python3
"""liril_ask.py — Quick CLI to consult LIRIL over NATS.

Usage:
    python liril_ask.py classify "build docker image"
    python liril_ask.py route "fix CI/CD pipeline"
    python liril_ask.py advise "green screen crash" --options "restart gpu,check driver,rollback"
    python liril_ask.py train "built voxel terrain" ART
    python liril_ask.py train --from-history          # batch-train all FORGE.md phases
    python liril_ask.py retrain
    python liril_ask.py status
    python liril_ask.py health
    python liril_ask.py infer "write a fibonacci function" --model fast
    python liril_ask.py infer "explain NATS JetStream" --model quality
    python liril_ask.py execute "build a terrain system" --bifocal
    python liril_ask.py bifocal "build a terrain system"   # shortcut: NPU→GPU0→GPU1 pipeline
    python liril_ask.py bench
    python liril_ask.py aurora
    python liril_ask.py mesh
    python liril_ask.py agents
    python liril_ask.py agents --timeout 10

BIFOCAL MODE (dual-GPU pipeline):
    GPU0 = mercury.infer       → Nemotron-Nano-9B  (fast intent classification, ~300ms)
    GPU1 = mercury.infer.quality → Mistral-Nemo-12B (full generation with refined intent, 2-8s)
    NPU  = Intel AI Boost        → domain classification (<5ms)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import warnings

# Auto-load .env from repo root so NATS_URL token is always available
_env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8", errors="replace") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# Prefer the host-side Docker NATS bus for CLI tools.
# Some Windows sessions keep a stale user NATS_URL pointed at the guest bus on 14222,
# which can return an invalid handshake for host-side CLI requests.
def _uniq(seq):
    seen = set()
    out = []
    for item in seq:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

NATS_CANDIDATES = _uniq([
    os.environ.get("NATS_URL_HOST"),
    os.environ.get("NATS_URL"),
    os.environ.get("TENET_NATS_URL"),
    "nats://127.0.0.1:4223",
    "nats://127.0.0.1:4223",
    "nats://127.0.0.1:4223",
])
NATS_URL = NATS_CANDIDATES[0]
HEALTH_PROBE_TASK = "liril nats docker pipeline service heartbeat telemetry"

# Python 3.14: ProactorEventLoop works with nats-py; SelectorEventLoop no longer needed.
# Keep the policy only for Python <=3.13 where ProactorEventLoop has known socket issues.
if sys.platform == "win32" and sys.version_info < (3, 14):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
TIMEOUT = 60.0

async def _connect_nats(connect_timeout: float = 5):
    import nats

    last_error = None
    for url in NATS_CANDIDATES:
        try:
            nc = await nats.connect(url, connect_timeout=connect_timeout, max_reconnect_attempts=1)
            globals()["NATS_URL"] = url
            return nc
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("nats: no servers available for connection")

async def _request(subject: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    nc = await _connect_nats(connect_timeout=5)
    try:
        msg = await nc.request(subject, json.dumps(payload).encode(), timeout=timeout)
        return json.loads(msg.data.decode())
    finally:
        await nc.drain()

async def _request_or_status_fallback(
    subject: str, payload: dict, timeout: float = TIMEOUT
) -> dict:
    nc = await _connect_nats(connect_timeout=5)
    try:
        try:
            msg = await nc.request(subject, json.dumps(payload).encode(), timeout=timeout)
            return json.loads(msg.data.decode())
        except Exception:
            future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

            async def _cb(msg):
                if not future.done():
                    try:
                        future.set_result(json.loads(msg.data.decode()))
                    except Exception as e:
                        future.set_exception(e)

            sub = await nc.subscribe("tenet5.liril.status", cb=_cb)
            try:
                try:
                    return await asyncio.wait_for(future, timeout=max(timeout, 35))
                except asyncio.TimeoutError:
                    msg = await nc.request(
                        "tenet5.liril.classify",
                        json.dumps({"task": HEALTH_PROBE_TASK}).encode(),
                        timeout=timeout,
                    )
                    return json.loads(msg.data.decode())
            finally:
                await sub.unsubscribe()
    finally:
        await nc.drain()

def _run(subject: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    return asyncio.run(_request(subject, payload, timeout))

def _print(data: dict) -> None:
    print(json.dumps(data, indent=2))

def cmd_classify(args):
    task = " ".join(args.task)
    result = _run("tenet5.liril.classify", {"task": task}, timeout=8)
    _print(result)

def cmd_route(args):
    task = " ".join(args.task)
    result = _run("tenet5.liril.route", {"task": task}, timeout=8)
    _print(result)

def cmd_execute(args):
    """Full pipeline: CLASSIFY → ROUTE → EXECUTE → TRAIN."""
    task    = " ".join(args.task)
    bifocal = getattr(args, "bifocal", False)
    result  = _run("tenet5.liril.execute", {"type": "execute", "task": task, "bifocal": bifocal})
    _print(result)

def cmd_bifocal(args):
    """BIFOCAL: NPU classify → GPU0 (Nemotron-Nano-9B) refine → GPU1 (Mistral-Nemo-12B) generate."""
    task = " ".join(args.task)

    print(f"[BIFOCAL] Starting dual-GPU pipeline...")
    print(f"  NPU    → classify domain")
    print(f"  GPU0   → mercury.infer (Nemotron-Nano-9B, fast intent refinement)")
    print(f"  GPU1   → mercury.infer.quality (Mistral-Nemo-12B, full generation)")
    print()

    t0 = time.time()
    result = _run("tenet5.liril.execute", {"type": "execute", "task": task, "bifocal": True}, timeout=180)
    total_ms = round((time.time() - t0) * 1000)

    # Print structured bifocal output
    pipeline_str = result.get("execution", {}).get("bifocal_pipeline", "") or result.get("pipeline", "")
    domain = result.get("classification", {}).get("domain", result.get("domain", "?"))
    print(f"[BIFOCAL] domain={domain}  total={total_ms}ms  pipeline={pipeline_str}")
    print()

    exec_r = result.get("execution", result)
    gpu0 = exec_r.get("gpu0", {})
    gpu1 = exec_r.get("gpu1", {})
    if gpu0:
        print(f"  GPU0 ({gpu0.get('model','nemotron-nano')})  {gpu0.get('latency_ms','?')}ms")
        parsed = gpu0.get("parsed", {})
        if parsed:
            print(f"    intent       = {parsed.get('intent','?')}")
            print(f"    refined_task = {parsed.get('refined_task','?')[:100]}")
            print(f"    context      = {parsed.get('context','?')[:100]}")
        print()

    if gpu1:
        print(f"  GPU1 ({gpu1.get('model','mistral-nemo')})  {gpu1.get('latency_ms','?')}ms  tokens={gpu1.get('tokens',0)}")
        print()

    text = exec_r.get("text") or exec_r.get("content") or exec_r.get("response", "")
    if text:
        print(text)
    else:
        _print(result)

def cmd_advise(args):
    context = " ".join(args.context)
    options = args.options.split(",") if args.options else []
    result = _run("tenet5.liril.advise", {"task": context, "options": options}, timeout=25.0)
    _print(result)

def cmd_train(args):
    # ── --from-history: batch-train from FORGE.md ─────────────────────────
    if getattr(args, "from_history", False):
        _train_from_forge()
        return

    task = " ".join(args.task) if args.task else ""
    if not task:
        print("Error: provide a task string or use --from-history", file=sys.stderr)
        sys.exit(1)
    domain = args.domain.upper() if args.domain else "TECHNOLOGY"
    payload = {"task": task, "domain": domain}
    async def _pub():
        nc = await _connect_nats(connect_timeout=5)
        try:
            resp = await nc.request("tenet5.liril.train", json.dumps(payload).encode(), timeout=8)
            return json.loads(resp.data.decode())
        except Exception:
            await nc.publish("tenet5.liril.train", json.dumps(payload).encode())
            await nc.drain()
            return {"status": "published", "domain": domain, "task": task}
        finally:
            await nc.drain()
    result = asyncio.run(_pub())
    _print(result)


def _train_from_forge():
    """Parse FORGE.md section headings and batch-train LIRIL from them."""
    import re
    import pathlib

    forge_path = pathlib.Path(__file__).parent.parent / "FORGE.md"
    if not forge_path.exists():
        print(f"FORGE.md not found at {forge_path}", file=sys.stderr)
        return

    text = forge_path.read_text(encoding="utf-8", errors="replace")

    _DOMAIN_KEYWORDS = {
        "ETHICS":      ["ethics", "human rights", "policy", "compliance", "legal", "rights"],
        "MATHEMATICS": ["math", "matrix", "algorithm", "equation", "seed", "fibonacci",
                        "benchmark", "latency", "score", "metric", "np vs n", "n vs np"],
        "ART":         ["art", "glass", "ui", "ux", "theme", "opacity", "visual", "color",
                        "design", "hud", "voxel", "holographic", "dashboard"],
        "SCIENCE":     ["science", "nats", "health", "signal", "hardware", "tpu", "gpu",
                        "openvino", "npu", "inference", "speculative"],
        "REASONING":   ["reasoning", "classify", "route", "sator", "liril", "train",
                        "dispatch", "orchestrat", "loom", "tenet"],
        "TECHNOLOGY":  ["technology", "docker", "deploy", "build", "cicd", "api", "rest",
                        "code", "python", "function", "git", "server", "mesh", "agent",
                        "nats", "deified", "nemoclaw", "aurora", "bridge", "mcp", "phase"],
    }

    def _infer_domain(txt: str) -> str:
        t = txt.lower()
        scores = {d: sum(1 for kw in kws if kw in t) for d, kws in _DOMAIN_KEYWORDS.items()}
        best = max(scores, key=scores.get)
        # Default to TECHNOLOGY if no keyword matched
        return best if scores[best] > 0 else "TECHNOLOGY"

    # Extract all ## level headings + first body line
    heading_re = re.compile(r"^## (.+?)$", re.MULTILINE)
    lines = text.splitlines()
    h2_positions = []
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            h2_positions.append((i, m.group(1).strip()))

    samples = []
    for idx, (pos, heading) in enumerate(h2_positions):
        # Strip emoji/unicode prefixes and date suffixes
        clean = re.sub(r"[^\w\s\-/+]", " ", heading)
        clean = re.sub(r"\s{2,}", " ", clean).strip()
        # Grab first non-empty body line as context
        body_lines = []
        end = h2_positions[idx+1][0] if idx+1 < len(h2_positions) else pos+20
        for l in lines[pos+1:min(pos+8, end)]:
            l = l.strip().lstrip("*-#> ")
            if l and not l.startswith("```"):
                body_lines.append(l[:100])
                if len(body_lines) >= 2:
                    break
        snippet = " ".join(body_lines)[:120]
        task_text = f"{clean}. {snippet}".strip(". ") if snippet else clean
        domain = _infer_domain(heading + " " + snippet)
        samples.append((task_text, domain))

    if not samples:
        print("No ## headings found in FORGE.md")
        return

    print(f"  Found {len(samples)} sections in FORGE.md")
    print(f"  Training LIRIL with {len(samples)} samples...")

    async def _train_batch(samples):
        nc = await _connect_nats(connect_timeout=5)
        ok = 0
        try:
            for task, domain in samples:
                payload = json.dumps({"task": task, "domain": domain}).encode()
                try:
                    await nc.request("tenet5.liril.train", payload, timeout=6)
                    ok += 1
                except Exception:
                    await nc.publish("tenet5.liril.train", payload)
                    ok += 1
                await asyncio.sleep(0.15)  # respect LIRIL NPU rate limiter
        finally:
            await nc.drain()
        return ok

    trained = asyncio.run(_train_batch(samples))
    print(f"  Trained: {trained}/{len(samples)} samples sent")

    from collections import Counter
    dist = Counter(d for _, d in samples)
    for dom, cnt in sorted(dist.items()):
        print(f"    {dom:<14} {cnt}")
    print()
    print("  Done. Trigger retrain with: python tools/liril_ask.py retrain")



def cmd_retrain(args):
    async def _pub():
        nc = await _connect_nats(connect_timeout=5)
        await nc.publish("tenet5.liril.retrain", b"{}")
        await nc.drain()
        return {"status": "retrain_triggered"}
    result = asyncio.run(_pub())
    _print(result)

def cmd_status(args):
    """Enhanced status: LIRIL NPU + last domain broadcast + NATS subject spot-checks."""
    import statistics

    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║  TENET5 LIRIL Status — SEED 118400                      ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print()

    # ── 1. NPU status ──────────────────────────────────────────────────────
    t0 = time.time()
    try:
        result = asyncio.run(
            _request_or_status_fallback("tenet5.liril.status", {}, timeout=8)
        )
        latency = round((time.time() - t0) * 1000)
        device  = result.get("device", "?")
        ready   = result.get("npu_ready", True)
        samples = result.get("training_samples", "?")
        method  = result.get("method", "?")
        npu_ok  = True
    except Exception as e:
        latency, device, ready, samples, method, npu_ok = 9999, "?", False, "?", "?", False

    status_icon = "✓" if npu_ok else "✗"
    print(f"  {status_icon} LIRIL NPU    device={device}  ready={ready}  method={method}")
    print(f"               samples={samples}  latency={latency}ms")
    print()

    # ── 2. Quick classify probe ────────────────────────────────────────────
    probes = [
        ("write python function",         "TECHNOLOGY"),
        ("generate a logo design",         "ART"),
        ("calculate prime factorization",  "MATHEMATICS"),
    ]
    print("  Domain classify spot-checks:")
    for task, expected in probes:
        t0 = time.time()
        try:
            r = _run("tenet5.liril.classify", {"task": task}, timeout=5)
            got  = r.get("domain", "?")
            conf = r.get("confidence", 0.0)
            ms   = round((time.time() - t0) * 1000)
            match = "✓" if got == expected else "~"
            print(f"    {match} [{got:<13}] {conf*100:>5.1f}%  {ms:>4}ms  \"{task[:35]}\"")
        except Exception as e:
            print(f"    ✗ [TIMEOUT    ] —      ?ms  \"{task[:35]}\"")
    print()

    # ── 3. Key NATS subjects spot-check ───────────────────────────────────
    subjects = [
        ("tenet5.liril.status",           b"{}",  "LIRIL NPU"),
        ("tenet5.mesh.status",            b"{}",  "Mesh Monitor"),
        ("tenet5.gpu_dashboard.status",   b"{}",  "GPU Dashboard"),
        ("tenet5.nemoclaw.health",        b"{}",  "NemoClaw Health"),
    ]
    print("  NATS subject health:")
    for subj, _payload, label in subjects:
        t0 = time.time()
        try:
            _run(subj, {}, timeout=4)
            ms = round((time.time() - t0) * 1000)
            print(f"    ✓ {label:<22}  {ms:>4}ms  {subj}")
        except Exception:
            print(f"    ✗ {label:<22}  TIMEOUT   {subj}")
    print()
    print(f"  NATS: {NATS_URL}")
    print()

def cmd_health(args):
    t0 = time.time()
    try:
        result = asyncio.run(
            _request_or_status_fallback("tenet5.liril.status", {}, timeout=5)
        )
        latency_ms = round((time.time() - t0) * 1000)
        device = result.get("device", "?")
        ready = result.get("npu_ready", False)
        print(f"LIRIL OK — device={device} ready={ready} — {latency_ms}ms")
    except Exception as e:
        print(f"LIRIL DOWN — {e}", file=sys.stderr)
        sys.exit(1)

def cmd_infer(args):
    """Send inference request to mercury.infer with model routing."""
    prompt = " ".join(args.prompt)
    model_flag = (args.model or "fast").lower()

    # Route: fast → mercury.infer (GPU0 primary), quality → mercury.infer.quality (GPU1)
    subject = "mercury.infer.quality" if model_flag == "quality" else "mercury.infer"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "seed": 118400,
    }

    t0 = time.time()
    result = _run(subject, payload, timeout=120)
    latency_ms = round((time.time() - t0) * 1000)

    model = result.get("model", "?")
    text = result.get("text") or result.get("content") or result.get("response", "")
    tokens = result.get("tokens", 0)

    # GPU attribution from model name
    if "gpu0" in model.lower() or model.endswith("-0"):
        gpu = "GPU0"
    elif "gpu1" in model.lower() or model.endswith("-1"):
        gpu = "GPU1"
    else:
        gpu = "GPU?"

    print(f"[{gpu}] model={model} tokens={tokens} latency={latency_ms}ms")
    print(f"subject={subject}")
    print()
    print(text)

def cmd_qa(args):
    """Run Playwright QA suite and publish results to NATS."""
    suite = args.suite
    # Import from sibling lirilclaw package
    qa_path = os.path.join(os.path.dirname(__file__), "lirilclaw")
    if qa_path not in sys.path:
        sys.path.insert(0, qa_path)
    from playwright_qa import run_qa
    reports = asyncio.run(run_qa(suite_name=suite, publish=True))
    total_failed = sum(r.failed for r in reports)
    if total_failed:
        sys.exit(1)

def cmd_bench(args):
    """Quick benchmark: LIRIL + GPU0 + GPU1 latencies."""
    import statistics
    print("TENET5 Quick Benchmark (SEED=118400)")
    print()

    # LIRIL classify
    times = []
    for txt in ["write python code", "design a logo", "prove theorem"]:
        t0 = time.time()
        try:
            _run("tenet5.liril.classify", {"task": txt}, timeout=5)
            times.append(round((time.time()-t0)*1000))
        except Exception:
            times.append(5000)
    valid = [t for t in times if t < 5000]
    print(f"  LIRIL NPU  p50={round(statistics.median(valid))}ms  min={min(valid)}ms  max={max(valid)}ms")

    # mercury.infer
    payload = {"messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 10}
    infer_times = []
    models_seen = []
    for _ in range(3):
        t0 = time.time()
        try:
            r = _run("mercury.infer", payload, timeout=30)
            infer_times.append(round((time.time()-t0)*1000))
            if r.get("model"): models_seen.append(r["model"])
        except Exception:
            infer_times.append(30000)
    valid_i = [t for t in infer_times if t < 30000]
    models_str = "/".join(set(models_seen))
    if valid_i:
        print(f"  mercury    p50={round(statistics.median(valid_i))}ms  min={min(valid_i)}ms  max={max(valid_i)}ms  models={models_str}")
    print()
    print("  Run 'python tools/gpu_benchmark.py' for full GPU0 vs GPU1 suite")

def cmd_agents(args):
    """List Aurora agent heartbeats from NATS tenet5.agents.heartbeat."""
    import asyncio as _asyncio

    COLLECT_SECS = float(getattr(args, "collect_secs", None) or
                         os.environ.get("AGENTS_COLLECT_SECS", "5"))
    _DOMAIN_COLORS = {
        "REASONING":   "\033[38;2;170;204;255m",
        "TECHNOLOGY":  "\033[38;2;0;170;255m",
        "ART":         "\033[38;2;255;136;170m",
        "SCIENCE":     "\033[38;2;0;238;136m",
        "ETHICS":      "\033[38;2;255;170;0m",
        "MATHEMATICS": "\033[38;2;170;136;255m",
    }
    RESET = "\033[0m"
    GREEN = "\033[38;2;0;238;136m"
    RED   = "\033[38;2;238;68;68m"
    DIM   = "\033[38;2;107;114;128m"

    print()
    print("  \u256c\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2556")
    print("  \u2551  TENET5 Aurora Agent Mesh                              \u2551")
    print("  \u2569\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
    print()
    print(f"  Listening for heartbeats ({COLLECT_SECS}s)...")
    print()

    collected: dict = {}

    async def _collect():
        try:
            nc = await _connect_nats(connect_timeout=5)
        except Exception as e:
            print(f"  {RED}\u2717 NATS connection failed: {e}{RESET}")
            return

        async def _on_hb(msg):
            try:
                hb = json.loads(msg.data.decode())
                name = hb.get("agent", "unknown")
                collected[name] = hb
            except Exception:
                pass

        await nc.subscribe("tenet5.agents.heartbeat", cb=_on_hb)
        await nc.subscribe("tenet5.agents.ready",     cb=_on_hb)
        await _asyncio.sleep(COLLECT_SECS)
        await nc.drain()

    _asyncio.run(_collect())

    if not collected:
        print(f"  {RED}\u2717 No agent heartbeats received in {COLLECT_SECS}s{RESET}")
        print(f"  {DIM}  \u2192 Start agents: .\\Start-Aurora.ps1 -Agents{RESET}")
        print()
        return

    import time as _time
    now = _time.time()
    print(f"  {'Agent':<18} {'Domain':<14} {'Status':<8} {'Tasks':>6}  {'Uptime':>8}  Last HB")
    print(f"  {'-'*18} {'-'*14} {'-'*8} {'-'*6}  {'-'*8}  {'-'*10}")
    for name, hb in sorted(collected.items()):
        domain  = hb.get("domain", "—")
        status  = hb.get("status", "online")
        tasks   = hb.get("tasks", 0)
        uptime  = hb.get("uptime_s", 0)
        ts      = hb.get("ts", now)
        age_s   = round(now - ts)
        dcol    = _DOMAIN_COLORS.get(domain, DIM)
        scol    = GREEN if status in ("online", "ready") else RED
        uptime_fmt = f"{int(uptime//60)}m{int(uptime%60)}s" if uptime >= 60 else f"{int(uptime)}s"
        print(f"  {name:<18} {dcol}{domain:<14}{RESET} {scol}{status:<8}{RESET} {tasks:>6}  {uptime_fmt:>8}  {age_s}s ago")
    print()
    online = sum(1 for h in collected.values() if h.get("status") in ("online","ready"))
    print(f"  Agents online: {online}/{len(collected)}  (SEED 118400)")
    print()


def cmd_mesh(args):
    """Check TENET5 service mesh health via NATS tenet5.mesh.*"""
    MESH_PORT = int(os.environ.get("MESH_PORT", "8088"))
    import urllib.request

    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║  TENET5 Service Mesh Status                              ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print()

    # ── NATS mesh probe ───────────────────────────────────────────────
    mesh_subjects = [
        ("tenet5.mesh.health",         "Mesh Health"),
        ("tenet5.mesh.status",         "Mesh Snapshot"),
        ("tenet5.liril.status",        "LIRIL NPU"),
        ("tenet5.gpu_dashboard.status","GPU Dashboard"),
        ("tenet5.nemoclaw.health",     "NemoClaw"),
    ]
    # HTTP-only probes (no NATS subject)
    http_probes = [
        ("http://127.0.0.1:11433/health", "DEIFIED API"),
    ]
    ok_count = 0
    total_count = len(mesh_subjects) + len(http_probes)
    agent_count = 0
    for subj, label in mesh_subjects:
        t0 = time.time()
        try:
            r = _run(subj, {}, timeout=3)
            ms = round((time.time() - t0) * 1000)
            extra = ""
            if isinstance(r, dict):
                if "services" in r:
                    extra = f"  ({len(r['services'])} services)"
                elif "agents" in r:
                    agent_count = len(r.get("agents", {}))
                    extra = f"  ({agent_count} agents)"
                elif "domain" in r:
                    extra = f"  domain={r['domain']}"
                elif "device" in r:
                    extra = f"  device={r.get('device','?')}"
            print(f"    \u2713 {label:<26}  {ms:>4}ms{extra}")
            ok_count += 1
        except Exception:
            print(f"    \u2717 {label:<26}  TIMEOUT/OFFLINE")
    # HTTP-only probes
    for url, label in http_probes:
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ms = round((time.time() - t0) * 1000)
                print(f"    \u2713 {label:<26}  {ms:>4}ms")
                ok_count += 1
        except Exception:
            print(f"    \u2717 {label:<26}  TIMEOUT/OFFLINE")
    print()
    print(f"  Services online: {ok_count}/{total_count}")
    print()

    # ── HTTP mesh page ────────────────────────────────────────────────
    t0 = time.time()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{MESH_PORT}/", timeout=3
        ) as resp:
            ms = round((time.time() - t0) * 1000)
            print(f"  \u2713 Mesh HTTP       http://127.0.0.1:{MESH_PORT}   {ms}ms")
    except Exception:
        print(f"  \u2717 Mesh HTTP       http://127.0.0.1:{MESH_PORT}   UNREACHABLE")
        print(f"    \u2192 Start with: python src/tenet/tenet5_mesh_monitor.py")
    print()


def cmd_aurora(args):
    """Check Aurora orchestrator (HTTP port 8520) + NATS agent registry."""
    import urllib.request
    import urllib.error
    AURORA_PORT = int(os.environ.get("AURORA_PORT", "8520"))
    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║  TENET5 Aurora Orchestrator Status                       ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print()

    # ── HTTP /api/status ──────────────────────────────────────────────
    t0 = time.time()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{AURORA_PORT}/api/status", timeout=4
        ) as resp:
            data = json.loads(resp.read().decode())
            ms = round((time.time() - t0) * 1000)
            print(f"  ✓ Aurora HTTP   http://127.0.0.1:{AURORA_PORT}   {ms}ms")
            agents = data.get("agents", {})
            liril  = data.get("liril", {})
            print(f"    Agents: {len(agents)}  LIRIL domain: {liril.get('domain','—')}  "
                  f"conf: {round(liril.get('confidence',0)*100)}%")
    except Exception as e:
        print(f"  ✗ Aurora HTTP   http://127.0.0.1:{AURORA_PORT}   UNREACHABLE ({e})")
        print(f"    → Start with: .\\Start-Aurora.ps1")
    print()

    # ── NATS agent mesh ───────────────────────────────────────────────
    print("  Agent mesh (NATS tenet5.agents.*):")
    agent_subs = [
        ("tenet5.liril.status",           "LIRIL NPU"),
        ("tenet5.gpu_dashboard.status",   "GPU Dashboard"),
        ("tenet5.mesh.status",            "Mesh Monitor"),
    ]
    for subj, label in agent_subs:
        t0 = time.time()
        try:
            _run(subj, {}, timeout=3)
            ms = round((time.time() - t0) * 1000)
            print(f"    ✓ {label:<22}  {ms:>4}ms")
        except Exception:
            print(f"    ✗ {label:<22}  TIMEOUT")
    print()
    print(f"  Aurora dashboard: http://127.0.0.1:{AURORA_PORT}")
    print(f"  Start agents:     .\\Start-Aurora.ps1 -Agents")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Consult LIRIL NPU classifier over NATS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("classify", help="Classify a task into ARTSTEM domain")
    p.add_argument("task", nargs="+")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("route", help="Get routing info (agent + workspace + IDE)")
    p.add_argument("task", nargs="+")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("execute", help="Full pipeline: CLASSIFY → ROUTE → EXECUTE → TRAIN")
    p.add_argument("task", nargs="+")
    p.add_argument("--bifocal", action="store_true", default=False,
                   help="Use bifocal dual-GPU mode: GPU0(Nemotron-Nano) refines, GPU1(Mistral-Nemo) generates")
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("advise", help="Get proactive recommendations")
    p.add_argument("context", nargs="+")
    p.add_argument("--options", default="", help="Comma-separated options to rank")
    p.set_defaults(func=cmd_advise)

    p = sub.add_parser("train", help="Submit a training sample (or batch-train from FORGE.md)")
    p.add_argument("task", nargs="*", help="Task text (omit with --from-history)")
    p.add_argument("domain", nargs="?", default="TECHNOLOGY",
                   help="ARTSTEM domain (ART, REASONING, TECHNOLOGY, etc). Ignored with --from-history")
    p.add_argument("--from-history", action="store_true", dest="from_history",
                   help="Parse FORGE.md Phase blocks and batch-train LIRIL from all phases")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("retrain", help="Force NPU model retrain")
    p.set_defaults(func=cmd_retrain)

    p = sub.add_parser("status", help="Quick status check via classify")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("health", help="Quick health check (exit 0=ok, 1=down)")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("infer", help="Send inference request via mercury.infer")
    p.add_argument("prompt", nargs="+")
    p.add_argument("--model", choices=["fast", "quality"], default="fast",
                   help="fast=GPU0 (mercury.infer), quality=GPU1 (mercury.infer.quality)")
    p.add_argument("--max-tokens", type=int, default=256, dest="max_tokens")
    p.set_defaults(func=cmd_infer)

    p = sub.add_parser("qa", help="Run Playwright QA suite (all|nats|docker|web|site|campaign|cicd)")
    p.add_argument("--suite", default="all",
                   choices=["all", "nats", "docker", "web", "site", "campaign", "cicd"],
                   help="Which QA suite to run")
    p.set_defaults(func=cmd_qa)

    p = sub.add_parser("bench", help="Quick LIRIL+GPU latency benchmark")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("aurora", help="Check Aurora orchestrator status (port 8520)")
    p.set_defaults(func=cmd_aurora)

    p = sub.add_parser("mesh", help="Check service mesh health via NATS tenet5.mesh.*")
    p.set_defaults(func=cmd_mesh)

    p = sub.add_parser("agents", help="List Aurora agent heartbeats (5s listen window)")
    p.add_argument("--timeout", type=float, default=5.0, dest="collect_secs",
                   help="How long to listen for heartbeats (default 5s)")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("bifocal",
                       help="BIFOCAL dual-GPU: NPU→GPU0(Nemotron-Nano refine)→GPU1(Mistral-Nemo generate)")
    p.add_argument("task", nargs="+", help="Task or prompt")
    p.set_defaults(func=cmd_bifocal)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

