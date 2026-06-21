# Orchestration Pipeline

A multi-agent software development pipeline that takes a task description and produces working, tested, committed code. Eleven specialized LLM agents run in sequence via a local FastAPI server, communicating through a shared blackboard. Claude drives the orchestration; all coding agents run DeepSeek V4 Pro.

---

## Why it exists

Manually prompting an LLM to write a full feature produces inconsistent results — the model loses context, skips tests, and doesn't enforce architecture. This pipeline solves that by decomposing the dev cycle into specialized agents with narrow, well-defined responsibilities, hard output contracts, and automatic retry/escalation logic.

---

## High-level architecture

```
Claude (orchestrator)
      │  HTTP /run, /run-batch
      ▼
FastAPI server (port 8765)
      │  dispatches agents
      ▼
Agent (DeepSeek V4 Pro)
      │  reads/writes
      ▼
Blackboard (.blackboard/ directory)
```

**Blackboard pattern** — all inter-agent communication goes through a shared `.blackboard/` directory inside the repo being built. Agents never call each other directly. Each agent reads its inputs from the blackboard and writes its outputs there. This keeps agents fully decoupled and makes every artifact inspectable at any point.

**FastAPI server** — `orchestration_server.py` exposes three key endpoints: `/run` (single agent), `/run-batch` (multiple agents in parallel), and `/blackboard` (read/write). The server is the only process that instantiates agents; Claude calls it over HTTP.

**Claude as orchestrator** — the `/orchestrate` skill in `.claude/skills/` drives the pipeline. Claude reads each agent's blackboard output after it runs, decides whether to advance, retry, or halt, and constructs the next agent's instruction. This keeps orchestration logic in a readable skill file rather than hardcoded.

---

## The 11-agent pipeline

```
architect → designer → planner → scaffolder → tester → reviewer → test-generator
    → coder → qa-tester → code-reviewer → commit → documentation
```

| Agent | Reads | Writes | Role |
|---|---|---|---|
| **architect** | task.md + codebase | analysis.md, requirements.md | Explores the repo, identifies stack and constraints, writes numbered requirements |
| **designer** | analysis.md, requirements.md | design-spec.md | Component hierarchy, design tokens, responsive breakpoints, accessibility — skips if backend-only |
| **planner** | analysis.md, requirements.md, design-spec.md | work-plan.md | Topological sort of work units into parallel batches; Batch 1 = contracts only |
| **scaffolder** | work-plan.md Batch 1 | contract files in repo, contracts.md | Writes interfaces, DTOs, types, entities — no business logic; compile-checks before advancing |
| **tester** | analysis.md, requirements.md, design-spec.md | test-plan.md | Writes plain-English test cases covering happy path, edge cases, error handling, auth/authz, E2E |
| **reviewer** | requirements.md, test-plan.md | test-review.md | Gates the test plan — VERDICT: PASS advances, VERDICT: FAIL sends tester back (max 3 retries) |
| **test-generator** | test-plan.md, test-review.md, contract files | test files in repo | Writes real assertions against Batch 1 contracts; uses read_contract_file (blocked from impls) |
| **coder** | requirements.md, work-plan.md Batch 2+, test files | source files in repo, implementation.md | Makes tests pass, starting from Batch 2 (Batch 1 already done by scaffolder) |
| **qa-tester** | requirements.md, implementation.md | qa-report.md | Adversarial testing — SQL injection, XSS, auth bypass, boundary values; VERDICT: PASS/FAIL |
| **code-reviewer** | requirements.md, all source files | code-review.md | Reviews correctness, edge cases, code quality; VERDICT: PASS/FAIL |
| **commit** | task.md, implementation.md | commit.md | Stages and commits all changes with conventional commit format |
| **documentation** | full blackboard + source files | docs.md, pr-description.md | User-facing docs and clean PR description |

---

## Key mechanisms

### Loop detection

Every agent invocation is tracked in `.blackboard/status.json`. Before running an agent, the server checks `current_iterations >= AGENT_MAX_CALLS[agent]`. If the limit is hit, the run returns `status: "escalated"` instead of calling the model. In delegated mode this auto-surfaces to the human; in server mode Claude decides whether to accept the best available output or halt.

```python
# config.py
AGENT_MAX_CALLS = {
    "coder": 5, "tester": 3, "reviewer": 3, "qa-tester": 3, ...
}
```

### Output validation

After every agent run, `validators.py` checks that the expected blackboard files exist, meet a minimum length, contain required structural markers, and have no unclosed code fences. On failure the server auto-retries once with a targeted completion instruction before returning the result.

```python
_CHECKS = {
    "planner":       {"work-plan.md":    {"min_len": 100, "required": ["## Batch"]}},
    "reviewer":      {"test-review.md":  {"min_len": 50,  "required": ["VERDICT:"]}},
    "code-reviewer": {"code-review.md":  {"min_len": 50,  "required": ["VERDICT:"]}},
    ...
}
```

### Parallel execution — two modes

**AGENT_CONCURRENCY** — same instruction, N instances, reconciler merges drafts. Used when more coverage of the same task is useful (e.g. 3 code-reviewers catch more issues than 1).

**`/run-batch`** — different instruction per instance, runs simultaneously. Used for the coder batches from `work-plan.md`: each work unit in a batch is a separate task with its own file scope. Wall time ≈ slowest task, not sum.

```powershell
# One call per batch — blocks until all tasks in the batch complete
$body = @{
    repo_path = "<path>"
    tasks = @(
        @{ agent = "coder"; instruction = "implement src/types/index.ts — req 1.1 ..." },
        @{ agent = "coder"; instruction = "implement prisma/schema.prisma — req 2.3 ..." }
    )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8765/run-batch" -Method POST -Body $body -ContentType "application/json"
```

### Parallel instance scope lock

When an agent runs as a parallel instance (via `/run-batch`), the base agent injects two constraints into its system prompt:

1. **Blackboard suffix** — every blackboard file it writes gets an auto-suffix (`-p1`, `-p2`, …) so files don't collide. `implementation.md` → `implementation-p1.md`.
2. **Source file scope lock** — the agent may only create or modify source files explicitly named in its instruction. Shared infrastructure files (`package.json`, `tsconfig.json`, `pom.xml`, etc.) are off-limits unless they are the explicitly assigned unit. Required new dependencies are documented in the blackboard file instead.

### Agent memory (episodic)

Each agent appends learnings to `agent-memory/<name>.md` on the blackboard at the end of its run — stack conventions, build commands, recurring patterns, gotchas. On the next invocation, `MemoryManager` reads this file and injects it into the agent's system prompt. This means the pipeline gets faster and more accurate on repos it has seen before.

### Thread safety

`BlackBoard.update_status` is a read-modify-write operation. A `threading.Lock` wraps every call so concurrent `/run-batch` workers can't corrupt `status.json`.

---

## Configuration (`config.py`)

| Key | Purpose |
|---|---|
| `AGENT_TOOL_ITERATIONS` | Max internal tool calls per agent run (coder=200, tester=60, …) |
| `AGENT_MAX_CALLS` | Max times orchestrator may invoke an agent before escalation (coder=5, reviewer=3, …) |
| `AGENT_CONCURRENCY` | How many same-instruction parallel instances for `/run` (default 1) |
| `AGENT_OUTPUTS` | Expected blackboard output files per agent — used by validators |

---

## Execution modes

| Mode | How to start | Who orchestrates |
|---|---|---|
| **Server** | `start.bat` → `/orchestrate` skill | Claude reads blackboard, drives pipeline via HTTP |
| **Auto** | `start.bat --mode auto --repo <path> "task"` | DeepSeek orchestrator agent, fully autonomous |
| **Delegated** | `start.bat --mode delegated --gates coder,qa --repo <path> "task"` | DeepSeek orchestrator + human approval gates at specified checkpoints |

---

## Failure handling

| Failure | Response |
|---|---|
| Agent output incomplete/truncated | Validator catches it, server retries once with targeted completion instruction |
| Agent exceeds `AGENT_MAX_CALLS` | Returns `status: escalated` — orchestrator decides to accept or halt |
| Reviewer/QA VERDICT: FAIL | Orchestrator loops back to the upstream agent (tester or coder) with the review as context |
| Reconciler output token-limited | `finish_reason == "length"` check marks output as `⚠ truncated` in summary |
| Parallel worker touches wrong file | Scope lock in system prompt prevents it; if violated, coder's done condition (build + tests pass) catches it |

---

## Repo structure

```
orchestration/
├── orchestration_server.py   # FastAPI server — /run, /run-batch, /blackboard, /status
├── agents/
│   ├── base.py               # BaseAgent: agentic loop, parallel instance injection, memory
│   ├── orchestrator.py       # OrchestratorAgent: pipeline tools, reconciler, run_agents_parallel
│   ├── delegated_orchestrator.py  # Adds human gate support and raise_to_user
│   ├── <name>.py             # One file per agent (thin — just sets NAME and READ_ONLY)
│   └── prompts/<name>.md     # Full agent prompt with job, rules, output format, done condition
├── blackboard.py             # BlackBoard: read/write/status with thread-safe update
├── config.py                 # AGENT_TOOL_ITERATIONS, AGENT_MAX_CALLS, AGENT_CONCURRENCY
├── validators.py             # Per-agent output completeness checks
├── memory.py                 # MemoryManager: reads agent-memory/<name>.md for episodic context
├── session.py                # SessionManager: persists message history for resume_session
├── tools.py                  # Tool schemas and ToolExecutor (list_files, read_file, write_file, …)
└── .claude/skills/
    ├── orchestrate/SKILL.md  # Step-by-step Claude orchestration instructions
    └── run-orchestration/    # Server start/stop/smoke-test
```
