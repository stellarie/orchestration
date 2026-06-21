---
name: run-orchestration
description: >
  Run, start, launch, build, or smoke-test the orchestration server. Use this skill
  whenever the user asks to start the server, check if it is running, verify the API,
  or confirm the orchestration server is healthy. Also use before invoking /orchestrate
  if the server may not be up.
---

# Run: Orchestration Server

A FastAPI server (`orchestration_server.py`) that exposes an 11-agent development pipeline
via HTTP on `127.0.0.1:8765`. Driven via `smoke.ps1` (start/stop/verify) or plain
`Invoke-RestMethod` calls.

## Prerequisites

```powershell
cd C:\Users\Stella\orchestration
python -m pip install -r requirements.txt
```

Requires `DEEPSEEK_API_KEY` in `.env` (see `.env.example`). The server starts without it
but agent `/run` calls will fail.

## Run (agent path)

**Start + smoke-verify:**
```powershell
cd C:\Users\Stella\orchestration
.\\.claude\skills\run-orchestration\smoke.ps1
```

**Health check only (server already running):**
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -Method GET
```

`/health` returning `ok` only means the server process is alive — it does NOT mean any in-flight
agent has finished. Agent runs continue server-side even after a client `Invoke-RestMethod` call times
out, so use `/health` to confirm the server survived, then read the blackboard to check actual progress.

When calling `/run` or `/run-batch` directly (not via smoke.ps1), always pass `-TimeoutSec 600` (single)
or `-TimeoutSec 900` (batch). The PowerShell default of 100s will abort mid-run.

**Stop:**
```powershell
.\\.claude\skills\run-orchestration\smoke.ps1 -Stop
```

The smoke script starts the server in the background, waits up to 10s for it to be ready,
hits `/health` and `/task/init`, then prints the PID. The PID is stored in
`.claude\skills\run-orchestration\server.pid` for the `-Stop` flag.

**The background server runs WITHOUT `--reload`.** If you edit any agent module, prompt, or
`orchestration_server.py` while it is running, the change will not take effect — stop and restart:
```powershell
.\\.claude\skills\run-orchestration\smoke.ps1 -Stop
.\\.claude\skills\run-orchestration\smoke.ps1
```
(Use the human path `start.bat` instead if you want `--reload` while actively editing agent code.)

## Run (human path)

```bat
start.bat
```

Opens a console window running uvicorn with `--reload`. Ctrl-C to stop. Not useful
headless.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness check |
| GET | `/docs` | Swagger UI |
| POST | `/task/init` | Create blackboard for a repo |
| POST | `/run` | Dispatch a single agent |
| POST | `/run-batch` | Dispatch multiple agents in parallel (blocks until all done) |
| GET | `/blackboard` | Read a blackboard file |
| POST | `/blackboard` | Write a blackboard file |
| GET | `/status` | Agent status summary |

Valid agent names: `architect`, `designer`, `planner`, `tester`, `reviewer`,
`test-generator`, `coder`, `qa-tester`, `code-reviewer`, `commit`, `documentation`.

## Drive the pipeline

Once the server is running, confirm `/health` returns `ok`, then use the `/orchestrate` skill to drive
the full agent pipeline. That skill handles all the `/run` dispatch logic, retry gates, and blackboard
reads. Do not dispatch agents before `/health` succeeds — a `/run` against a not-yet-ready server fails.

## Gotchas

- `uvicorn.exe` is installed to `AppData\Roaming\Python\Python314-32\Scripts` which is
  NOT on PATH. Use `python -m uvicorn ...` not bare `uvicorn`.
- The skill dir is 3 levels deep inside `orchestration\`, so `$PSScriptRoot\..\..\..\`
  resolves back to the project root. Using `..\..\` lands you in `.claude\` instead.
- Port 8765 is hardcoded in `orchestration_server.py` and `start.bat`. No env override.
- Agents that run builds (via `run_command`) spawn long-lived daemons — e.g. the **Gradle daemon**
  holds `.jar` files open in the target repo. These outlive the agent run and will block deleting or
  cleaning the repo with "file in use" errors. Stop them before wiping a repo: `./gradlew --stop`
  (Gradle), or kill the stray `java`/`node` processes that the build started.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `uvicorn: command not found` | Use `python -m uvicorn` |
| Server not ready in 10s | Check `.env` exists; uvicorn may crash on import if deps missing |
| Agent `/run` fails with 500 | `DEEPSEEK_API_KEY` is missing or invalid in `.env` |
| Client call times out / exit 137 | Not a failure — the agent runs on. Re-check `/health`, then read the blackboard before retrying. Add `-TimeoutSec 600`/`900`. |
| Port 8765 already in use on start | A previous server is still up. `-Stop` first, or kill the PID in `server.pid`. |
| Edited agent/server code but behavior unchanged | Background server has no `--reload` — `-Stop` then restart. |
| Can't delete the target repo ("file in use") | A build daemon spawned by an agent holds locks — `./gradlew --stop` or kill stray `java`/`node`. |
