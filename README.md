# Orchestration

A multi-pipeline AI system built on specialized LLM agents. Three pipelines cover the full dev loop: **research** (deep web research + analysis), **scout** (OSS contribution discovery), and **dev** (12-agent software development). All pipelines can be chained — research feeds into dev, scout discovers a repo and dev contributes to it.

---

## Pipelines at a glance

### Dev pipeline
Takes a task description and produces working, tested, committed code.

```
architect → designer → planner → scaffolder → tester → reviewer
  → test-generator → coder → qa-tester → code-reviewer → commit → documentation
```

### Research pipeline
Deep web research on any topic, always followed by an analyst and action planner.

```
query-planner → searcher×3 → reader×3 → tech-auditor → research-synthesizer
  → research-analyst → action-planner
```

Deliverables (`brief.md`, `versions.md`, `caveats.md`, `analysis.md`, `action-plan.md`) are written to `~/stella-research/<topic-slug>/` and pushed to the `stellarie/stella-research` GitHub repo after the run completes.

### Scout pipeline
Finds open-source projects worth contributing to, scopes the issue, and queues a dev pipeline against the cloned repo.

```
oss-scout → issue-auditor → contribution-planner → [queues dev pipeline]
```

---

## How it works

A FastAPI server (`orchestration_server.py`) hosts the pipeline runner and all agents, exposing a REST + SSE API. A Preact frontend (`frontend/index.html`) lets you start and monitor all three pipelines in real time.

### The blackboard

All inter-agent communication goes through a `.blackboard/` directory inside the target repo. Agents write outputs there and read each other's outputs from there — no agent calls another agent directly.

### Research output

Deliverable-producing research agents write to two places simultaneously:

1. `.blackboard/research/` — for downstream agents to read during the same run
2. `~/stella-research/<topic-slug>/` — clean output that gets committed and pushed to `stellarie/stella-research` after the pipeline finishes

The topic slug is derived automatically from the research description (e.g. *"React 19 migration guide"* → `react-19-migration-guide`). Each research run gets its own subfolder, so the repo accumulates a browsable history of all past research.

### The judge

After each pipeline step, a **Judge** agent reads the expected output files from the blackboard and issues one of three verdicts:

- `PASS` — advances to the next step
- `REWORK agent: critique` — replays the step with targeted feedback (up to 5 attempts)
- `ESCALATE: reason` — pauses the pipeline for human review (or auto-passes in full-auto mode)

### Global agent memory

Every agent has a global memory file in `orchestration/agent-memory/<name>.md`. After completing work, agents call `write_memory` to record learnings — patterns, pitfalls, domain rules, judge critique patterns — that persist across all projects and future runs. Memory is injected into the agent's system prompt on every invocation and auto-compacted (via DeepSeek) when it exceeds 200 000 characters.

### Pipeline chaining

Research and scout pipelines can auto-queue a dev pipeline on completion. The research pipeline writes a `handoff.md` summary regardless of whether it succeeded fully — the dev pipeline always gets whatever context was produced.

---

## Getting started

### Prerequisites

- Python 3.11+
- `DEEPSEEK_API_KEY` — required (coding agents, memory compaction)
- `ANTHROPIC_API_KEY` — optional; enables Claude Opus/Sonnet for architect, planner, judge, and all research agents. Falls back to DeepSeek v4 Pro if unset (or auto-inherited when running inside Claude Code)
- `TAVILY_API_KEY` — optional; enables higher-quality web search for research/scout agents. Falls back to DuckDuckGo (via `ddgs`, no key needed) if unset
- `GITHUB_TOKEN` — optional; required to push research deliverables to `stellarie/stella-research`. Needs `contents: write` on that repo. Token is also used to clone the repo on first run if `~/stella-research` doesn't exist yet.
- `GIT_USER_NAME` — optional; name used for git commits in `~/stella-research` (e.g. `Stella`). Required for push to work if no global git identity is configured.
- `GIT_USER_EMAIL` — optional; email used for git commits in `~/stella-research`. Required alongside `GIT_USER_NAME`.

### Install

```bash
cd orchestration
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

```env
DEEPSEEK_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here   # optional but recommended
TAVILY_API_KEY=your-key-here      # optional
GITHUB_TOKEN=your-token-here      # optional — push research to stellarie/stella-research
GIT_USER_NAME=Your Name           # optional — git commit identity for stella-research pushes
GIT_USER_EMAIL=you@example.com    # optional — git commit identity for stella-research pushes
```

### Start

```bat
start.bat
```

The server listens on `http://127.0.0.1:8765`. Open `frontend/index.html` in a browser to access the UI.

### Run

From the UI, click **New Pipeline** to start a dev run, **Research + Dev** for a research-first run, or **OSS Scout** to find a contribution target.

From Claude Code, use the `/orchestrate` skill for the dev pipeline.

---

## Logs

Each run produces two files in `orchestration/logs/`:

- `orchestration.log` — rotating server log (agent calls, judge verdicts, errors)
- `<run-id>.log` — per-run thinking + tool-call log

---

## Configuration

All tuning lives in `config.py`:

| Key | Purpose |
|-----|---------|
| `MODELS` | Model + provider + thinking config per agent |
| `AGENT_TOOL_ITERATIONS` | Max tool calls per agent run (coder=200, judge=15, …) |
| `AGENT_CAPABILITIES` | Exact tool set + write-deny path patterns per agent |
| `AGENT_OUTPUTS` | Expected blackboard output files per agent — used by the judge |

### Agent prompts

All system prompts live in `agents/prompts/` as plain Markdown files. Edit them directly to adjust behaviour, add project conventions, or change output formats.

---

## Project structure

```
orchestration/
├── agents/
│   ├── prompts/              # System prompt for each agent (Markdown)
│   ├── base.py               # BaseAgent: agentic loop, stop signal, memory, dual-provider API
│   └── *.py                  # One file per agent
├── agent-memory/             # Global per-agent memory files (cross-project, auto-compacted)
├── docs/
│   └── pipeline.md           # Deep-dive architecture reference
├── frontend/
│   └── index.html            # Preact UI — pipeline management, live step graph, SSE stream
├── .claude/skills/
│   ├── orchestrate/          # /orchestrate skill for Claude Code
│   └── run-orchestration/    # Server start/health check
├── config.py                 # Models, capabilities, iterations, outputs
├── tools.py                  # Tool schemas + ToolExecutor (blackboard, output, memory, search, …)
├── blackboard.py             # Shared state store (.blackboard/)
├── memory.py                 # MemoryManager: global cross-project agent memory + auto-compaction
├── session.py                # SessionManager: message history for resume_session
├── orchestration_server.py   # FastAPI server — all pipeline endpoints + SSE bus
├── requirements.txt
└── start.bat

~/stella-research/            # Git repo (cloned from stellarie/stella-research on first run)
└── <topic-slug>/             # One folder per research run, auto-named from the description
    ├── brief.md
    ├── versions.md
    ├── caveats.md
    ├── analysis.md
    └── action-plan.md
```
