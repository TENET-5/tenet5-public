# S.L.A.T.E — SATOR Square Aligned Architecture
# SATORAREPOTENETOPERAROTAS — 25 letters, palindrome, N at center = 42

## The SATOR Square Maps to the Codebase

```
  S A T O R     →  SATOR/   Infrastructure, config, boot
  A R E P O     →  AREPO/   Routing, NATS, agent mesh
  T E [N] E T   →  TENET/   Core kernel, cube, compiler (N = fixed point)
  O P E R A     →  OPERA/   GPU, NPU, NemoClaw, inference
  R O T A S     →  ROTAS/   CI/CD, API, telemetry, deploy
      ✝         →  LIRIL/   The CROSS — orchestrator through all 5 rows
```

## Directory Structure

```
E:\S.L.A.T.E\
├── SATOR/                  # Row 0: Infrastructure
│   ├── config/             # System config, .env, NATS config
│   ├── boot/               # Boot scripts, heartbeat, serve
│   └── shared/             # Shared memory, file watcher, logging
├── AREPO/                  # Row 1: Routing & Messaging
│   ├── nats/               # NATS pool, logic gates, inference workers
│   ├── mesh/               # Agent mesh, orchestrator, dispatcher
│   ├── transport/          # Transport layer, mobile bridge
│   └── mcp/                # Session, phone, level MCP servers
├── TENET/                  # Row 2: Core Kernel (N at center)
│   ├── kernel/             # Kernel, core, SATOR grid MCP, gate engine
│   ├── cube/               # 5×5×5 cube, cube gates
│   ├── compiler/           # Crystal DAG, LOOM, NVSNP, ABCXYZ
│   ├── discoveries/        # Phase 8-26 discoveries
│   └── os/                 # Tensor offload, OS layer
├── OPERA/                  # Row 3: Compute & Inference
│   ├── gpu/                # GPU monitor, compute backend, CUDA
│   ├── npu/                # NPU bridge, OpenVINO
│   ├── nemoclaw/           # All NemoClaw modules (codegen, daemon, etc.)
│   ├── inference/          # Inference engine, NemoServer, rotor
│   └── racecar/            # RACECAR compute dispatcher
├── ROTAS/                  # Row 4: Output & Deploy
│   ├── cicd/               # CI/CD pipeline, health monitor
│   ├── api/                # TENET5 API, webapp, Vibe, Omniverse
│   └── telemetry/          # Mesh monitor, pipeline telemetry, audit trail
├── LIRIL/                  # The CROSS: Orchestrator
│   ├── mcp/                # LIRIL MCP server, LirilClaw, GPU advise
│   ├── training/           # Training analytics, ARTSTEM, embeddings
│   ├── ethics/             # Ethics gate
│   ├── repl/               # LIRIL talk, Vibe API
│   └── npu/                # LIRIL NPU service
├── rs/                     # Rust crates
├── cpp/                    # C++ modules
├── go/                     # Go modules
├── hs/                     # Haskell modules
├── q/                      # Q/kdb+ code
├── tests/                  # All tests (unit, integration, benchmark)
├── tools/                  # MCP tools
├── docs/                   # Documentation
└── bin/                    # Built binaries
```

## Constants

- SYSTEM_SEED = 118400 (370 × 64 × 5)
- IDENTITY = 42 = 0b101010
- Grid = 5×5×5 = 125 cells
- SATOR bands = 5
- Agent count = 5
- Fixed point = N (position [2,2])
- 127.0.0.1 ONLY — no external network
- UTF-8 encoding everywhere
- CUDA_VISIBLE_DEVICES = 0,1 (dual RTX 5070 Ti)

## Hard-Banned Software

1. OLLAMA — permanently banned
2. QWEN models — banned
3. CPU inference — banned (GPU+NPU only)

## NATS Communication (host port 4223, Docker guest 14222)

All services communicate via NATS JetStream:
- mercury.infer, mercury.infer.code — GPU inference
- npu.classify, npu.embed — NPU ops
- tenet5.liril.{classify,route,execute,train,sync,advise} — LIRIL orchestrator
- tenet5.lirilclaw.{dispatch,status,workspace} — watchdog
- tenet5.nemoclaw.codegen.{task,status} — code generation
- tenet5.empirical.{submit,results} — batch GPU dispatch

## Boot Order (follows SATOR rows)

1. SATOR: NATS server, config
2. AREPO: Agent mesh binds
3. TENET: Kernel activates, N = fixed point
4. OPERA: GPU inference (llama-server), NemoServer, NPU bridge
5. ROTAS: API endpoints, CI/CD
✝ LIRIL: Orchestrator fires last, crosses all 5 rows

Run: `powershell -File E:\S.L.A.T.E\Boot_SLATE.ps1`

## 6-Stack Languages

| Stack | Language | SATOR Row | Role |
|-------|----------|-----------|------|
| Core | Haskell | TENET [2,2] | Pure symbolic kernel |
| Exec | Rust | SATOR [0,0] | Memory-safe algorithms |
| Grid | Q/kdb+ | ROTAS [4,4] | Vector/matrix ops |
| Concurrency | Go | AREPO [1,1] | Microservices, NATS |
| Infra | C++ | OPERA [3,3] | GPU compute, CUDA |
| Sandbox | Python | ARCANUM | Prototyping, glue |

## Component Naming

ALL component names MUST be palindromes:
LIRIL, RACECAR, LEVEL, DEED, RADAR, KAYAK, CIVIC, ROTOR, SOLOS, MADAM, REFER, SAGAS, STATS, TENET
