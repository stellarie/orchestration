# Orchestration

A 12-agent AI pipeline that takes a task description and produces working, tested, committed code. Each agent has a single responsibility; they communicate through a shared blackboard rather than calling each other directly.

---

## How it works

```
architect → designer → planner → scaffolder → tester → reviewer
         → test-generator → coder → qa-tester → code-reviewer → commit → documentation
```

A FastAPI server (`orchestration_server.py`) hosts the agents and exposes a REST API. Claude Code acts as the **master orchestrator** — it drives the pipeline by calling the server between steps, reviewing each agent's output, and deciding whether to advance, patch, or retry.

### The blackboard

All inter-agent communication goes through a `.blackboard/` directory inside the target repo. Agents write their outputs there (`analysis.md`, `contracts.md`, `work-plan.md`, etc.) and read each other's outputs from there. No agent calls another agent directly.

### The pipeline steps

| # | Agent | Writes | Reads |
|---|---|---|---|
| 1 | **Architect** | `analysis.md`, `requirements.md`, `conventions.md` | `task.md` + codebase |
| 2 | **Designer** | `design-spec.md` | architect outputs |
| 3 | **Planner** | `work-plan.md` (TICKET-NNN batches) | architect + designer outputs |
| 4 | **Scaffolder** | Contract files, CI workflow, test env | `work-plan.md` Batch 1 |
| 5 | **Tester** | `test-plan.md` | requirements + design |
| 6 | **Reviewer** | `test-review.md` (VERDICT: PASS/FAIL) | test plan |
| 7 | **Test-Generator** | Test source files | contracts + test plan |
| 8 | **Coder** | Production source files (one batch at a time) | contracts + conventions |
| 9 | **QA-Tester** | `qa-report.md` (VERDICT: PASS/FAIL) | full codebase |
| 10 | **Code-Reviewer** | `code-review.md` (VERDICT: PASS/FAIL) | full codebase |
| 11 | **Commit** | `commit.md` | work-plan (for ticket IDs) |
| 12 | **Documentation** | `docs.md`, `pr-description.md` | full blackboard |

Each agent also writes a `checklist/<agent>.md` to the blackboard. The orchestrator reads it before advancing — any unchecked `- [ ]` item triggers a retry.

### TDD enforcement

After every coder batch:
1. **Compile check** — if it fails, the coder fixes it before anything else runs.
2. **Test check** — if any test fails, the coder fixes it before the next batch starts.

The test-generator writes real assertions against interfaces that don't exist yet (intentional compile errors). The coder implements against those tests, not the other way around.

### Models

| Agent | Model | Reasoning |
|---|---|---|
| Architect | Claude Opus 4.8 | Extended thinking, 16 000 token budget |
| Planner | Claude Sonnet 4.6 | Extended thinking, 10 000 token budget |
| Designer, Reviewer, QA-Tester, Code-Reviewer, Orchestrator | DeepSeek v4 Pro | Thinking, max effort |
| All others | DeepSeek v4 Pro | No extended thinking |

If `ANTHROPIC_API_KEY` is not set, architect and planner fall back to DeepSeek v4 Pro with `thinking=max`. When running inside a Claude Code session, the key is inherited automatically — no `.env` entry needed.

---

## Getting started

### 1. Prerequisites

- Python 3.11+
- A `DEEPSEEK_API_KEY` (required)
- An `ANTHROPIC_API_KEY` (optional — enables Claude Opus/Sonnet for architect/planner)
- Claude Code (for the `/orchestrate` skill)

### 2. Install dependencies

```bash
cd orchestration
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
DEEPSEEK_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here   # optional
```

### 4. Start the server

```bat
start.bat
```

Or directly:

```bash
python orchestration_server.py
```

The server listens on `http://127.0.0.1:8765`.

### 5. Run the pipeline

From Claude Code, use the `/orchestrate` skill. It will ask for a repo path and task description, then drive the full 12-step pipeline.

---

## Logs

Each pipeline run produces two files in `orchestration/logs/`:

- `orchestration.log` — the rotating server log (all agent API calls, validation results, errors)
- `<run-id>.log` — the thinking + tool-call log for that run, e.g.:

```
--- 2026-06-21 10:23:45 [architect]
I need to understand the project structure first. The task mentions a Spring Boot
application, so I'll start by listing files to find the build system...
---
2026-06-21 10:23:52 [architect] → list_files
2026-06-21 10:23:53 [architect] → read_file
--- 2026-06-21 10:23:55 [architect]
Found build.gradle — Gradle + Java 17. Base package from existing code is
com.example.checkin. Setting Namespace Declarations accordingly...
---
```

The `logs/` directory is gitignored.

---

## What to adjust before using

### Hardcoded paths in skills

The Claude Code skills (`.claude/skills/`) reference the server location directly. Update these two files to match where you cloned the repo:

**`.claude/skills/orchestrate/SKILL.md`** — one line:
```
tell the user to run `start.bat` in `C:\Users\Stella\orchestration\` first.
```

**`.claude/skills/run-orchestration/SKILL.md`** — two lines:
```
cd C:\Users\Stella\orchestration
```

Replace `C:\Users\Stella\orchestration` with your actual path. On macOS/Linux use forward slashes and drop the drive letter.

### Model selection

Edit `MODELS` in `config.py` to change which model each agent uses. Each entry has:

```python
"architect": {
    "model":        "claude-opus-4-8",   # model ID
    "provider":     "anthropic",          # "anthropic" | "deepseek"
    "thinking":     True,                 # enable extended reasoning
    "budget_tokens": 16000,               # anthropic only: thinking token budget
}
```

For DeepSeek agents, `"reasoning_effort": "max"` controls thinking depth (`"high"` | `"max"`).

### Agent tool limits

`AGENT_TOOL_ITERATIONS` in `config.py` controls how many tool calls each agent can make before the loop terminates. The coder is set highest (200) since it iterates through many files per batch. Lower these if you want faster (but shallower) runs.

### Access control

`AGENT_CAPABILITIES` in `config.py` declares exactly which tools each agent can use and which file paths it can write to. For example, the coder is blocked from writing test files and the test-generator is blocked from writing production source. Adjust these if your project layout differs from the defaults (e.g. different test directory names).

### Agent prompts

All agent system prompts live in `agents/prompts/`. They are plain Markdown files — edit them directly to adjust agent behaviour, add project-specific conventions, or change output formats. The validator checks in `validators.py` mirror the format requirements in the prompts; if you change an output format, update both.

---

## Project structure

```
orchestration/
├── agents/
│   ├── prompts/          # System prompt for each agent (Markdown)
│   ├── base.py           # BaseAgent: agentic loop, dual-provider API, logging
│   ├── orchestrator.py   # Delegated orchestrator agent
│   └── *.py              # One file per agent
├── .claude/
│   └── skills/
│       ├── orchestrate/  # /orchestrate skill for Claude Code
│       └── run-orchestration/
├── config.py             # Models, capabilities, concurrency, iterations
├── tools.py              # Tool schemas, ToolExecutor, path enforcement
├── validators.py         # Blackboard output validation per agent
├── blackboard.py         # Shared state store
├── orchestration_server.py
├── requirements.txt
└── start.bat
```
