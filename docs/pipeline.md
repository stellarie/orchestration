# Orchestration Pipeline — Architecture Reference

Three pipelines share a common infrastructure: a FastAPI server, a blackboard-based agent communication pattern, a judge gate after each step, and a global per-agent memory system.

---

## Architecture overview

```
Browser (Preact UI)
     │  SSE stream + REST
     ▼
FastAPI server  (:8765)
     │  dispatches pipeline steps
     ▼
Agent (Anthropic / DeepSeek)
     │  reads / writes
     ├──▶ .blackboard/           inter-agent scratch space
     └──▶ research/ or oss/      clean deliverable output
```

**Blackboard pattern** — all inter-agent communication goes through `.blackboard/` inside the target repo. No agent calls another agent directly. Every artifact is inspectable at any point during a run.

**Output directory** — deliverable-producing agents write the same content twice: once to `.blackboard/research/` (so downstream agents can read it) and once to `research/` or `oss/` at the repo root (so it can be pushed to GitHub or handed to the user as a clean artifact).

**SSE event bus** — the server emits real-time events over a `/events` SSE stream: `agent_start`, `agent_done`, `agent_stopped`, `tool_call`, `thinking`, `pipeline_status`, `pipeline_queued`, `research_pushed`. The UI consumes these to render the live step graph.

---

## Dev pipeline

### Agents

```
architect → designer → planner → scaffolder → tester → reviewer
  → test-generator → coder → qa-tester → code-reviewer → commit → documentation
```

| # | Agent | Reads | Writes |
|---|-------|-------|--------|
| 1 | **architect** | task.md + codebase | analysis.md, requirements.md, conventions.md |
| 2 | **designer** | architect outputs | design-spec.md |
| 3 | **planner** | architect + designer outputs | work-plan.md (TICKET-NNN batches) |
| 4 | **scaffolder** | work-plan.md Batch 1 | contract files in repo, contracts.md |
| 5 | **tester** | requirements + design | test-plan.md |
| 6 | **reviewer** | requirements + test plan | test-review.md (VERDICT: PASS/FAIL) |
| 7 | **test-generator** | contracts + test plan | test source files |
| 8 | **coder** | contracts + conventions + tests | production source files, implementation.md |
| 9 | **qa-tester** | full codebase | qa-report.md (VERDICT: PASS/FAIL) |
| 10 | **code-reviewer** | full codebase | code-review.md (VERDICT: PASS/FAIL) |
| 11 | **commit** | work-plan + implementation.md | commit.md |
| 12 | **documentation** | full blackboard + source | docs.md, pr-description.md |

### TDD enforcement

After every coder batch:
1. **Compile check** — if it fails, the coder fixes it before anything else runs.
2. **Test check** — if any test fails, the coder fixes it before the next batch starts.

The test-generator writes real assertions against interfaces that don't exist yet (intentional compile errors). The coder implements against those tests, not the other way around.

### Parallel execution

Two modes:

- **AGENT_CONCURRENCY** — same instruction, N instances in parallel, a reconciler merges drafts into a single canonical file. Used when broader coverage is useful (e.g. 3 architects with different angles).
- **Fan-out steps** — a list of agents run simultaneously (e.g. 3 searchers, 3 readers). Each instance writes to suffixed blackboard files (`search-results-p1.md`, `search-results-p2.md`, …). The parallel instance scope lock (injected into the system prompt) prevents collisions on shared infrastructure files.

---

## Research pipeline

Deep web research on any topic, always followed by analysis and action planning.

### Agents

```
query-planner → searcher×3 → reader×3 → tech-auditor
  → research-synthesizer → research-analyst → action-planner
```

| Agent | Blackboard (inter-agent) | Output file (deliverable) | Role |
|-------|--------------------------|---------------------------|------|
| **query-planner** | research/queries.md | — | Decomposes topic into 5–12 targeted search queries |
| **searcher** (×3) | research/search-results-p{N}.md | — | Executes assigned queries via web search |
| **reader** (×3) | research/extracted-p{N}.md | — | Fetches top URLs, extracts relevant content |
| **tech-auditor** | research/versions.md, research/caveats.md | research/versions.md, research/caveats.md | Current versions, breaking changes, deprecated APIs |
| **research-synthesizer** | research/brief.md | research/brief.md | Synthesizes all findings into a single actionable brief |
| **research-analyst** | research/analysis.md | research/analysis.md | Confidence map, gaps, contradictions, risks, recency |
| **action-planner** | research/action-plan.md | research/action-plan.md | Prioritized, specific, verifiable action items |

Searcher and reader agents are intermediate — they write only to the blackboard. The final four agents (tech-auditor, synthesizer, analyst, action-planner) write to **both** the blackboard and the output directory so their deliverables can be pushed to GitHub.

### GitHub push

After a successful research run, if a GitHub output is configured, the server reads from `{repo_path}/research/` (the output directory, not the blackboard) and pushes each deliverable file via the GitHub Contents API. If no output files exist, a warning is logged but the run is not marked as failed.

```
PUT https://api.github.com/repos/{owner}/{repo}/contents/{folder}/{filename}
Authorization: Bearer {GITHUB_TOKEN}
```

### Dev pipeline handoff

If "auto-queue dev pipeline" is enabled, the dev pipeline is queued immediately when the research run is started. A `handoff.md` summary is written to the blackboard after research completes regardless of success or failure — the dev pipeline always receives whatever context was produced, even if research stopped partway.

---

## Scout pipeline

Finds open-source repositories worth contributing to, scopes the best issue, and hands off to a dev pipeline.

### Agents

```
oss-scout → issue-auditor → contribution-planner → [queues dev pipeline against cloned repo]
```

| Agent | Blackboard | Output | Role |
|-------|------------|--------|------|
| **oss-scout** | scout/repos.md | oss/repos.md | GitHub + web search for contribution-friendly projects |
| **issue-auditor** | scout/target-issue.md, scout/runner-up.md | oss/target-issue.md, oss/runner-up.md | Evaluates open issues; picks the best tractable target |
| **contribution-planner** | scout/contribution-brief.md, scout/cloned_repo_path.md | oss/contribution-brief.md | Clones the repo, scopes the fix, writes an implementation brief |

`contribution-planner` writes the cloned repo path to `scout/cloned_repo_path.md`. The queued dev pipeline reads this to route itself to the cloned repo rather than the workspace, using the contribution brief as its task description.

---

## The judge

After every single-agent step across all pipelines, the **judge** runs before advancing.

```python
# _run_judge in orchestration_server.py
instr = (
    "Step 1: call read_blackboard(filename='task.md')\n"
    "Step 2: call read_blackboard for each output file\n"
    "Return PASS, REWORK {agent}: {critique}, or ESCALATE: {reason}."
)
```

The judge is told explicitly which `read_blackboard` calls to make for each agent (derived from `AGENT_OUTPUTS` in config.py). It verifies that files exist and are substantive, not just that the agent returned a response.

| Verdict | Effect |
|---------|--------|
| `PASS` | Advance to next step |
| `REWORK agent: critique` | Replay agent with targeted feedback; up to 5 attempts |
| `ESCALATE: reason` | In full-auto mode: auto-pass with a caveat note. In manual mode: pause pipeline for human review |

After exhausting max attempts without a PASS, the pipeline auto-passes with a `judge-caveats/<agent>.md` note and continues.

---

## Global agent memory

Every agent has a persistent memory file at `orchestration/agent-memory/<name>.md`. This is **global** — shared across all projects and pipeline runs, not per-repo.

### Reading

On every agent invocation, `MemoryManager` reads the memory file and injects its contents into the system prompt:

```
## Your global learnings (across all projects)
<contents of agent-memory/<name>.md>
```

### Writing

Agents have access to a `write_memory` tool. After completing their work, they record specific, concrete learnings — patterns that worked, pitfalls encountered, judge critique patterns, domain rules, version-specific facts.

### Auto-compaction

After each `write_memory` call, if the file exceeds **200 000 characters** (~50 000 tokens), the server calls DeepSeek to summarize it down to ~40 000 characters. The compacted version replaces the original and is tagged with a timestamp. This keeps memory useful without consuming the full context window.

---

## Tool system

Each agent is given exactly the tools listed in its `AGENT_CAPABILITIES` entry in `config.py`. The model can only call tools it sees in the schema — capabilities are the primary security gate.

| Tool | Available to | Purpose |
|------|-------------|---------|
| `read_file` | Most agents | Read source files from the repo |
| `write_file` | Coder, scaffolder, test-generator, qa-tester, commit | Write source files (subject to `write_deny` path patterns) |
| `list_files` | Most agents | List files in the repo |
| `read_blackboard` | All agents | Read from `.blackboard/` |
| `write_blackboard` | All agents | Write to `.blackboard/` |
| `write_output` | Deliverable agents | Write to `{repo_path}/research/` or `oss/` with auto-injected metadata header |
| `write_memory` | All agents | Append a learning to the global memory file |
| `run_command` | Coder, scaffolder, qa-tester, commit, contribution-planner | Shell commands in the repo root |
| `web_search` | Searcher, tech-auditor, oss-scout | Web search (Tavily if key set, else DuckDuckGo via `ddgs`) |
| `fetch_url` | Reader, tech-auditor, issue-auditor | Fetch and strip HTML from a URL |
| `github_search` | Oss-scout, issue-auditor | Search GitHub repos/issues via the REST API |

### Write output metadata

`write_output` automatically prepends a YAML frontmatter header to every deliverable file:

```yaml
---
agent: research-synthesizer
pipeline: a3f9c1d2
generated: 2026-06-23T14:32:01
---
```

### Web search backends

`web_search` uses the best available backend in priority order:

1. **Tavily** — if `TAVILY_API_KEY` is set. Higher result quality.
2. **DuckDuckGo** — via the `ddgs` package. No API key or registration required.

If neither is available, a clear install instruction is returned rather than a silent empty result.

---

## Pipeline stop

Any in-flight pipeline can be stopped from the UI or via `POST /pipeline/{id}/control` with `{"action": "stop"}`.

Each pipeline has a `threading.Event` (`stop_event`) that is set when a stop is requested. The base agent polls this event every 300 ms while waiting for an API response. When fired, the current API call is abandoned (the daemon thread completes harmlessly and its result is discarded), and `PipelineStoppedError` is raised. The agentic loop catches it and returns `{"status": "stopped"}`. The orchestrator emits an `agent_stopped` SSE event and the pipeline halts cleanly.

---

## Pipeline queue

Research and scout pipelines can auto-queue a dev pipeline. Queued pipelines wait in `_pipeline_queues[repo_path]` and start automatically when their predecessor emits `pipeline_status: done/failed/stopped` (via `_start_next_queued`). The source pipeline's `handoff.md` is injected into the queued pipeline's task description before it starts.

---

## Models

| Agent(s) | Model | Reasoning |
|----------|-------|-----------|
| Architect, reconciler, judge | Claude Opus 4.8 | Extended thinking, 16 000 token budget |
| Planner, consultant | Claude Sonnet 4.6 | Extended thinking, 10 000 / 8 000 token budget |
| All research + scout agents | Claude Opus 4.8 | Extended thinking, 108 000 / 32 000 token budget |
| All dev agents (coder, designer, …) | DeepSeek v4 Pro | Thinking, max effort where applicable |

If `ANTHROPIC_API_KEY` is not set, Anthropic agents fall back to DeepSeek v4 Pro with `thinking=max`. Inside a Claude Code session the key is inherited automatically.

---

## Configuration reference (`config.py`)

| Key | Purpose |
|-----|---------|
| `MODELS` | Model ID, provider, thinking config per agent |
| `AGENT_TOOL_ITERATIONS` | Max tool calls per agent run |
| `AGENT_CAPABILITIES` | Tool allowlist + write-deny path patterns per agent |
| `AGENT_OUTPUTS` | Blackboard files the judge checks after each agent step |

---

## Repo structure

```
orchestration/
├── orchestration_server.py     # FastAPI server — all pipeline logic, SSE bus, GitHub push
├── agents/
│   ├── base.py                 # BaseAgent: agentic loop, stop signal, memory, dual-provider API
│   ├── <name>.py               # One file per agent (sets NAME)
│   └── prompts/<name>.md       # Full agent prompt — job, process, output format, done condition
├── agent-memory/               # Global agent memory files (cross-project, auto-compacted)
├── blackboard.py               # BlackBoard: read/write/mkdir for .blackboard/
├── memory.py                   # MemoryManager: global read/append + auto-compaction
├── tools.py                    # Tool schemas + ToolExecutor (all tool implementations)
├── config.py                   # MODELS, AGENT_TOOL_ITERATIONS, AGENT_CAPABILITIES, AGENT_OUTPUTS
├── session.py                  # SessionManager: message history persistence
├── frontend/
│   └── index.html              # Preact UI: pipeline tabs, live step graph, SSE stream
└── docs/
    └── pipeline.md             # This file
```
