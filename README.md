# TENET5 · LIRIL — Public Review Snapshot

**This is a sanitized, read-only snapshot of the TENET5 + LIRIL codebase,
intended for AI-assisted code review (Grok, Claude, Gemini, etc.).**

The canonical private repository is `TENET-5/S.L.A.T.E`. This snapshot
deliberately excludes:
- Binary model weights
- OSINT data corpora
- API tokens, .env files, credentials
- Runtime caches, sqlite databases
- Large generated files
- Docker / infra definitions

What's here is **just the LIRIL tool code + the hydrogen runtime + the
doc layer** — roughly 76 files, ~29k lines.

## Layout

```
tools/       LIRIL 17-daemon stack + all supported skills + verify + status
hydrogen/    SLATE hydrogen (H1) binary-atom runtime + 13-integration probe
docs/        SLATE_CLAUDE.md, TENET5_CLAUDE.md, LIRIL_UNIFICATION.md, CSS + JS bootstrap
```

## What LIRIL is

LIRIL is the orchestrator that crosses all 5 rows of the SATOR square
grid (SATOR, AREPO, TENET, OPERA, ROTAS). In production she runs as
17 supervised daemons with:

- **Cap#1** Windows System Monitoring (30s metric pulse + NPU anomaly detection)
- **Cap#2** Service Control (dry-run default, hard denylist)
- **Cap#3** Process Management (PID-required mutations, reuse defense)
- **Cap#4** Driver Management (class-based denylist)
- **Cap#5** Windows Patch Management (MsrcSeverity-primary classification)
- **Cap#6** Autonomous Self-Repair (rule engine, consults journal for past outcomes)
- **Cap#8** Hardware Health Telemetry (GPU/NPU/SMART thresholds → fse)
- **Cap#9** User-Intent Prediction (observational, informs other caps)
- **Cap#10** Fail-Safe Escalation Protocol (meta-safety gate)
- Plus: supervisor, observer, network_reach, communication,
  file_awareness, journal, api, autonomous cron, goals (AGI substrate)

Every mutating capability goes through: denylist → dry-run → fse-gate
→ allowlist → plan-publish → veto-window → re-check → execute.

## What's been built recently

- 17 daemons supervised 24/7 with pidfile-based adoption across restarts
- Unified HTTP/JSON API on 127.0.0.1:18120 (22 routes)
- End-to-end verification: 42/42 assertions (`tools/liril_prove_all.py`)
- 13/13 hydrogen integrations (`hydrogen/hydrogen_runtime.py --check-all`)
- Persistent-memory journal with FTS5 search
- First cross-cap consumption: self_repair consults journal for past repair outcomes
- Autonomous cron with 13 jobs covering LIRIL/TENET5 OS/Docker/OSINT/website

## What reviewers should look for

1. **Safety invariants** that could be bypassed in fringe cases
2. **Composition bugs** across daemons (one daemon's write racing another's read)
3. **Subprocess / NATS error paths** that silently swallow failures
4. **Concurrency** — event loops, asyncio callbacks, sqlite locks
5. **Security** — SQL injection, path traversal, command injection in PowerShell-bridged code
6. **Resource leaks** — long-running daemons, unclosed file handles, NATS subscriptions

## Not for use as

- A deployable package (many modules depend on the broader TENET5 venv + NATS bus on :4223)
- A security audit target with claims of completeness — this is a snapshot, not the full repo

## Licence

EOSL-2.0 (see upstream). Copyright (c) 2024-2026 Daniel Perry.
All rights reserved. Review-only; redistribution not permitted.
