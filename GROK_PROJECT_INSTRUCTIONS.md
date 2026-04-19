# TENET5 + LIRIL — Grok Project Instructions

**Purpose:** Grok Heavy reviews and improves the LIRIL orchestrator stack
(the canonical private repo is `TENET-5/S.L.A.T.E`; this public mirror
`TENET-5/tenet5-public` is a sanitized review snapshot).

**Owner:** Daniel Perry (CEO, non-coder). Grok reviews code for Daniel
to approve — not to merge autonomously.

---

## 1 · Identity lock (non-negotiable)

- The project is **TENET5**. The AI is **LIRIL**. These names NEVER change.
- Brand subtitle: *Powered by LIRIL AI · NVIDIA · Intel*. Don't strip the NVIDIA or Intel mentions.
- Canonical seed: `SYSTEM_SEED = 118400 = 370 × 64 × 5`. Public constant.
- SATOR grid is `SATORAREPOTENETOPERAROTAS` — 25 letters, palindrome, N at centre.
- The heraldic crest (Crown + Red Shield + Gold Maple Leaf) is the permanent logo.

**Never suggest** renaming to "Canadian Accountability Project", "CAP",
"Investigation Platform", "Automation System", etc. The name is final.

---

## 2 · Architecture snapshot

LIRIL is the cross that threads through all 5 SATOR rows:

| Row | Letter | Role | Examples in this repo |
|---|---|---|---|
| 0 | SATOR | Workspace/infra | `liril_supervisor`, `liril_status`, `liril_api` |
| 1 | AREPO | Transport / NATS | `liril_observer`, `liril_network_reach` |
| 2 | TENET | Core (N center) | `liril_fail_safe_escalation`, `liril_journal`, `liril_goals` |
| 3 | OPERA | GPU/NPU inference | `nemo_server` references, `liril_hardware_health` |
| 4 | ROTAS | Output / deploy | `liril_prove_all`, `liril_autonomous` |

17 daemons run 24/7 under `tools/liril_supervisor.py`:

```
failsafe · observer · windows_monitor · service_control · process_manager
driver_manager · patch_manager · self_repair · hardware_health · user_intent
nemo_server · network_reach · communication · file_awareness · journal · api
autonomous
```

Plus hydrogen (H1 binary atom): 13 runtime integrations verified by
`hydrogen/hydrogen_runtime.py --check-all`.

---

## 3 · Mandatory safety invariants — DO NOT remove or weaken

Every mutating capability goes through this pipeline, in order:

```
denylist        →  cheapest hard-refuse (class/name/pattern match)
dry_run check   →  if EXEC_GATE unset, return "dry_run_logged"
fse gate        →  if level ≥ 3, return "refused_by_failsafe"
allowlist       →  user-curated; must match
NATS plan publish →  audit trail + other agents can veto
3-s veto window →  human/agent veto over NATS
re-check state  →  target may have changed (PID reuse, INF gone, etc.)
execute         →  real mutation
post-exec audit →  publish outcome, journal
```

**Never suggest**:
- Removing any step
- Auto-pushing to git
- Removing rate limits
- Removing dry-run default
- Removing the `--no-verify` prohibition (hooks must run)
- Merging straight to `main` without the jules_review_pr gate
- Enabling `RealTime` process priority (refused even if allowlisted)
- Killing `svchost.exe`, `lsass.exe`, `MsMpEng.exe`, or anything on the
  class-based denylist
- Uninstalling any driver whose `Class Name` is Display / Net / System /
  USB / SCSIAdapter / HDC / Keyboard / Mouse / HIDClass

---

## 4 · Composition rules

- Capabilities consume each other via the **journal** (`liril_journal.py`).
  `remember()` / `recall()` / `search()` are the cross-cap channel.
  Tags follow a taxonomy: `pref:` / `pattern:` / `decision:` / `incident:` /
  `observation:`.
- Capabilities communicate via **NATS on 127.0.0.1:4223**. Subject prefix
  conventions:
    - `tenet5.liril.<x>` — LIRIL-internal
    - `windows.<cap>.<stream>` — Cap#1–#5 per-domain
    - `tenet.111.<x>` — Gastown SATOR 111³ grid
- **HTTP API surface** is `127.0.0.1:18120` (see `liril_api.py`). Every
  cap has a read route; destructive routes are deliberately NOT exposed
  over HTTP. Grok should not suggest adding them.

---

## 5 · Hard-banned software (do not suggest)

1. **OLLAMA** — banned. Port 11434/11435 are never legitimate targets.
2. **Qwen models** — banned.
3. **CPU-only inference** — banned. Everything runs on GPU+NPU.
4. **vLLM** — deprecated.

The canonical GPU inference stack is `llama-server` (from llama.cpp)
on ports 8082 (GPU0) and 8083 (GPU1) bridged via NemoServer on NATS.

---

## 6 · Review output expectations

When reviewing a file, produce:

**TL;DR** — one sentence.

**Top findings** — numbered list, each:
- `SEVERITY: critical|high|medium|low|nit`
- `FILE:LINE_RANGE` (not just file)
- One-paragraph explanation
- **Concrete fix** — working code diff, not "consider doing X"

**Safety review** — any invariant-breakers? Explicit yes/no.

**Over/under-engineered** — any section that's doing too much / too
little for its purpose?

**Skip**: style debates, naming preferences, "you should use pydantic",
anything that's pure taste. Daniel hasn't asked for those.

Keep the response actionable — `git apply`-able diffs are ideal.

---

## 7 · Things Grok should actively look for

- **Subprocess calls without `CREATE_NO_WINDOW`** (pop console windows on the desktop; rule established 2026-04-18)
- **`asyncio.run()` inside request handlers** (creates a new event loop per call, Windows ProactorLoop has edge cases)
- **Unclosed NATS subscriptions** (will leak connections over hours)
- **SQLite without `timeout=` or with long-held locks** in daemon mode
- **Missing `encoding='utf-8'` on file I/O** (this codebase is UTF-8
  everywhere by convention)
- **`subprocess.run()` without `timeout=`** (will hang forever)
- **Environment variable reads without defaults** (`os.environ["X"]` will
  raise KeyError instead of defaulting)
- **PowerShell command injection** via f-string (service names / PIDs /
  driver INF paths passed directly into `Get-Service -Name '{x}'`)
- **Missing `veto_window` check** after the 3-s NATS veto wait in any
  new mutating capability

---

## 8 · Response length

Daniel reads fast. Keep responses under ~600 lines total unless a fix
is large. No restating-the-question openings. No "I've carefully
considered..." preambles. Just the findings.

---

## 9 · When in doubt

Favour the existing pattern over a clever new one. The codebase
prioritises "obvious in 3 years" over "elegant today". If Grok sees a
dead-simple loop that could be a clever comprehension, leave the loop.
If it sees a clever comprehension that could be a dead-simple loop,
suggest the loop.

---

*Last updated: 2026-04-19.*
