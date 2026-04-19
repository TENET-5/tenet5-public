from tenet.win_asyncio import run as _win_run  # Windows nats-py fix
#!/usr/bin/env python3
"""
LIRIL Training Seed — Bootstrap LIRIL classifier with quality domain examples.

Sends 10 high-quality training examples per domain via NATS so LIRIL
achieves >0.85 confidence on first classification after startup.

Domains: ART, TECHNOLOGY, SCIENCE, REASONING, ETHICS, MATHEMATICS

SYSTEM_SEED=118400
"""
import asyncio
import json
import os
import sys

SYSTEM_SEED = 118400

SEED_EXAMPLES = {
    "ART": [
        "design a glassmorphic UI with copper palette and golden ratio layout",
        "create a procedural texture shader with noise functions for terrain",
        "generate a voxel art style character with pixel palette COPPER colors",
        "compose ambient soundtrack layers for the dark-and-light game environment",
        "design animated particle system for spell casting visual effects",
        "create blueprint-style technical diagram with grid layout and precise motion",
        "render a cinematic camera path through the SATOR grid architecture",
        "design responsive CSS with glassmorphic cards and layered depth",
        "create avatar asset pipeline from concept sketch to Three.js mesh",
        "compose color palette for TENET5 brand: deep blue, copper, slate grey",
    ],
    "TECHNOLOGY": [
        "build NATS pub/sub pipeline for real-time game event telemetry",
        "deploy Docker container with health check and NATS mesh connectivity",
        "write Python async NATS subscriber with queue group load balancing",
        "configure llama-server GPU inference with main-gpu 0 and ctx-size 4096",
        "implement WebSocket bridge from browser to NATS Docker bus port 14222",
        "create CI/CD pipeline with 18 phases including ghost path integrity check",
        "fix CUDA DLL loading for llama-cpp on Windows with torch DLL preload shim",
        "write Vite build config for Three.js voxel engine with ES module exports",
        "configure NATS JetStream with max_mem 256MB and tenet5 stream subjects",
        "build agentic code generation pipeline with classify route execute train",
    ],
    "SCIENCE": [
        "measure RTX 5070 Ti GPU temperature and VRAM utilization at inference load",
        "benchmark NATS messaging throughput at 18000 messages per second on loopback",
        "observe MediaPipe LlmInference token rate on Tensor G3 Edge TPU Mali-G715",
        "collect latency samples for llama3-3b inference p50 p90 p99 on RTX 5070 Ti",
        "experiment with SATOR grid symmetry properties in a 5x5 letter arrangement",
        "record multi-probe hash classifier accuracy over 100 domain classification runs",
        "observe ABCXYZRouter throughput at 3.98 million operations per second",
        "investigate loom theory convergence behavior with increasing lock count",
        "measure system resonance at 118400 ticks divided by 118.4 Hz equals 1000",
        "observe mercury.infer response latency distribution across GPU0 and GPU1",
        "test hypothesis: adding more training samples reduces LIRIL classification error",
        "record NATS connection count and message rate under sustained load testing",
    ],
    "REASONING": [
        "if all A are B and all B are C then deduce all A are C by transitivity",
        "modus ponens: given P implies Q is true and P is true therefore Q must be true",
        "analyze this logical syllogism for validity: all M are P all S are M so all S are P",
        "apply De Morgan law to simplify NOT A AND NOT B into NOT A OR NOT B",
        "identify the logical fallacy: all birds fly penguins are birds penguins fly",
        "what is the contrapositive of if it rains then the ground is wet",
        "trace the deductive chain: premise A leads to B, B implies C, therefore A implies C",
        "determine which argument form is valid: denying the antecedent or modus tollens",
        "evaluate whether this argument commits the fallacy of affirming the consequent",
        "reason step by step: if no X are Y and all Z are Y then no Z are X",
    ],
    "ETHICS": [
        "ensure AI system respects privacy when logging NATS game event telemetry",
        "evaluate responsible deployment of autonomous code generation pipelines",
        "consider fairness in LIRIL domain classification across all 6 categories",
        "assess safety protocols for GPU inference with user-provided prompts",
        "review data retention policy for LIRIL training samples and session history",
        "establish consent framework for mobile phone LIRIL bridge to desktop AI",
        "evaluate bias in multi-probe hash classifier domain routing decisions",
        "ensure copyright compliance when NemoClaw generates TENET5 ecosystem code",
        "assess privacy implications of NemoClaw game telemetry tracking player actions",
        "review ethical use of autonomous CI/CD pipeline with 18 automated phases",
    ],
    "MATHEMATICS": [
        "compute SYSTEM_SEED 118400 divided by TICK_RATE 118.4 equals exactly 1000.0",
        "verify SATOR grid palindrome SATOR-AREPO-TENET-OPERA-ROTAS mathematical proof",
        "calculate loom theory 111 cubed equals 1367631 with 125 locks convergence",
        "prove ABCXYZRouter resonance function returns 14018560 for seed 118400",
        "compute TriDAG hash for TENET5 5x5x5 logic enforcement authorization",
        "verify N vs NP millennium problem polynomial time complexity bounds",
        "calculate LIRIL position coordinates at row 4 column 0 in SATOR grid",
        "prove softmax separates domain confidence: formula sum exp(x_i/T) normalization",
        "compute multi-probe hash with 16 hash functions across 6 domain buckets",
        "verify fibonacci sequence convergence in loom thread lock calculation",
        "derive the closed-form equation for ABCXYZRouter resonance at seed 118400",
        "compute the exact integer sum of all SATOR grid letter ordinal values",
    ],
}


async def seed_liril(nats_url: str = "nats://127.0.0.1:14222") -> None:
    try:
        import nats
    except ImportError:
        print("nats-py not installed")
        sys.exit(1)

    print(f"Connecting to LIRIL at {nats_url}...")
    nc = await nats.connect(nats_url, connect_timeout=10)
    print(f"Connected. Seeding {sum(len(v) for v in SEED_EXAMPLES.values())} examples...")
    print()

    total = 0
    for domain, examples in SEED_EXAMPLES.items():
        ok = 0
        for task in examples:
            payload = json.dumps({
                "task": task,
                "result": {
                    "domain": domain,
                    "status": "seeded",
                    "quality": 0.95,
                    "agent": "seed_script",
                },
            }).encode()
            try:
                resp = await nc.request("tenet5.liril.train", payload, timeout=5)
                result = json.loads(resp.data)
                if result.get("trained"):
                    ok += 1
                    total += 1
            except Exception as e:
                print(f"  WARN [{domain}]: {e}")
            await asyncio.sleep(0.15)  # throttle (must exceed 0.1s rate limit in liril_npu_service)
        print(f"  {domain:12s}: {ok}/{len(examples)} seeded")

    print()
    print(f"Total seeded: {total}/{sum(len(v) for v in SEED_EXAMPLES.values())}")
    print()

    # Verify confidence improved
    test_cases = [
        ("paint a copper sunset landscape", "ART"),
        ("write Python NATS subscriber", "TECHNOLOGY"),
        ("measure GPU inference latency samples", "SCIENCE"),
        ("if P implies Q and P is true then Q follows by modus ponens", "REASONING"),
        ("ensure AI respects user privacy", "ETHICS"),
        ("compute SATOR palindrome hash", "MATHEMATICS"),
    ]

    print("Post-seed confidence check:")
    for task, expected in test_cases:
        try:
            resp = await nc.request(
                "tenet5.liril.classify",
                json.dumps({"task": task}).encode(),
                timeout=5,
            )
            d = json.loads(resp.data)
            domain = d.get("domain", "?")
            conf = d.get("confidence", 0)
            match = "OK" if domain == expected else f"MISMATCH (expected {expected})"
            print(f"  {task[:40]:<40} → {domain:12s} conf={conf:.3f} {match}")
        except Exception as e:
            print(f"  {task[:40]:<40} → FAIL: {e}")

    await nc.close()
    print()
    print(f"LIRIL seed complete. SEED={SYSTEM_SEED}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    nats_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NATS_URL", "nats://127.0.0.1:14222")
    _win_run(seed_liril(nats_url))
