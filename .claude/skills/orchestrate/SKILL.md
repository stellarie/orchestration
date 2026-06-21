---
name: orchestrate
description: >
  Multi-agent software development orchestration pipeline. Use this skill whenever
  the user invokes /orchestrate, wants to run the agents on a task, says "let the
  pipeline handle this", "delegate this to the agents", "build this feature using the
  orchestration system", or asks to orchestrate any development task. Drives a 12-agent
  pipeline (architect, designer, planner, scaffolder, tester, reviewer, test-generator, coder,
  qa-tester, code-reviewer, commit, documentation) via a local Python server. Always use
  this skill for any orchestration request, even if the user just says "orchestrate this"
  with no other context.
---

# Orchestrate

You are the **master orchestrator**. Drive a 12-agent development pipeline by dispatching
agents via a local Python server and making all loop/advance decisions yourself between steps.

---

## Pre-flight

Check the server is running and capture the Anthropic availability flag:

```powershell
$health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -Method GET
$anthropicAvailable = $health.anthropic_available
```

If the call fails, tell the user to run `start.bat` in `C:\Users\Stella\orchestration\` first.

When `$anthropicAvailable` is `$false`, architect and planner steps run inline (you perform the role directly and write outputs via `POST /blackboard`). All other agents are unaffected.

---

## Setup

Confirm `repo_path` (default: cwd) and `task` description with the user. The task description becomes
`task.md` and is the architect's primary input, so make it self-contained: state the goal, the stack,
hard constraints, the end condition, and the **absolute paths** of any spec/design docs the architect
should read (the architect is instructed to open them).

The pipeline uses two AI providers. `DEEPSEEK_API_KEY` is required. `ANTHROPIC_API_KEY` is optional
but strongly recommended:

| Key | Required | Effect if missing |
|---|---|---|
| `DEEPSEEK_API_KEY` | **Yes** — hard failure without it | Server won't start |
| `ANTHROPIC_API_KEY` | No | Architect + planner fall back to DeepSeek v4 Pro with `thinking=max`. When running inside Claude Code the key is inherited automatically from the session — no `.env` entry needed. |

If the repo will be pushed to GitHub, confirm that `GH_TOKEN` (or `GITHUB_TOKEN`) is set in the
environment — the scaffolder writes `.github/workflows/ci.yml` and CI will fire on the first push.
No token is needed for the pipeline itself; it is only needed if you want to verify CI triggered after commit.

Then init:

```powershell
$body = @{ repo_path = "<repo_path>"; description = "<task>" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8765/task/init" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 600
```

---

## Run a single agent

```powershell
$body = @{
    agent          = "<name>"
    repo_path      = "<repo_path>"
    instruction    = "<instruction text>"
    resume_session = $false   # set $true on retries
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8765/run" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 600
```

**Always pass `-TimeoutSec 600` for single-agent runs.** The PowerShell default (100s) aborts mid-run.

**A client-side timeout (or exit 137 / "operation has timed out") does NOT mean the agent failed.**
The agent keeps running server-side and writes to the blackboard/filesystem regardless of whether
your HTTP call is still listening. **Never re-dispatch on a timeout alone.** Before retrying:
1. Hit `/health` to confirm the server is alive.
2. Read the agent's expected blackboard file (or `list_files` the repo) to confirm whether the work landed.
3. Only re-dispatch if the output is **genuinely missing or incomplete**. Re-running a succeeded agent
   wastes a cycle and can clobber good output. Checking file existence takes 5 seconds; a wasted
   re-dispatch costs 10+ minutes.

## Run multiple agents in parallel (`/run-batch`)

Use this for independent work units that can execute simultaneously (e.g. a batch from work-plan.md).
Each task automatically gets a suffix (-p1, -p2, …) so blackboard files do not collide.
The call blocks until **all** tasks complete and returns a `results` array + `wall_time`.

```powershell
$body = @{
    repo_path = "<repo_path>"
    tasks = @(
        @{ agent = "coder"; instruction = "Implement src/types/index.ts: User and Product interfaces" },
        @{ agent = "coder"; instruction = "Implement prisma/schema.prisma: User and Product models" },
        @{ agent = "coder"; instruction = "Implement src/config/env.ts: validate DATABASE_URL and JWT_SECRET" }
    )
} | ConvertTo-Json -Depth 5
$result = Invoke-RestMethod -Uri "http://127.0.0.1:8765/run-batch" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 1200
# $result.wall_time  — total elapsed seconds (≈ slowest task, not sum)
# $result.results    — per-task status and output
```

Use `-TimeoutSec 1200` for batches — coder units on a Java/Spring project routinely take 8–15 minutes
each and batches compound. If the client call times out, the batch is still running server-side.
**Do not re-dispatch.** Wait, then `list_files` the repo to confirm which units' files landed.
Re-run only the units whose output files are genuinely missing, via `/run` with `resume_session: $true`.

Check the logs to confirm tasks actually ran in parallel:
- Different `thread=` values on START lines → truly concurrent
- `wall_time` ≈ slowest individual task (not sum) → parallelism working

## Read blackboard

```powershell
$r = Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard?repo_path=<repo_path>&filename=<file>" -Method GET
$content = $r.content
```

Blackboard files can be large. To read a verdict, slice the tail rather than dumping the whole file:
```powershell
($r.content -split "`n") | Select-Object -Last 15
```

---

## Per-step review protocol

After **every** agent run, apply this three-tier review before advancing. Do not skip it — semantic
gaps found early cost one retry; gaps found at code-review cost three coder retries.

| Tier | When | Action |
|---|---|---|
| **PASS** | Output is semantically complete and correct | Advance immediately |
| **PATCH** | Minor, self-contained gap you can fix in one sentence | Write the correction directly to the blackboard via `POST /blackboard`, then advance |
| **RETRY** | Major gap, wrong approach, or missing critical section | Re-run the agent with the specific gap as context |

### What counts as PATCH vs RETRY per agent

**Architect** — PATCH: one vague requirement (rewrite it); missing one field in a constraint list.
RETRY: wrong stack identified; entire section missing; requirements are feature-wish-list not testable specs.

**Designer** — PATCH: one design token undefined; one component missing a state description.
RETRY: component hierarchy missing; backend project has no API contract documented.

**Planner** — PATCH: one batch unit missing its coding instruction; minor description typo.
RETRY: dependency order violation (unit in batch N depends on unit in batch N); a requirement maps to zero work units.

**Tester** — PATCH: one test missing its expected outcome; a test description is ambiguous but fixable.
RETRY: an entire requirement category is untested; no auth/authz tests despite a protected resource existing.

**Test-generator** — PATCH: one file contains a stub assertion — re-dispatch that file only with `resume_session: false`.
RETRY: systemic stubbing across all test files.

**Coder** — PASS only if the batch compile check passes (see `## Batch compile check`).
RETRY if compile errors exist — re-run the specific failing unit with the errors as context.

**QA-tester / Reviewer / Code-reviewer** — act only on the explicit `VERDICT:` token; do not infer from prose.
Never PATCH a verdict agent — always re-run if the output is unclear or the verdict is missing.

### How to PATCH

```powershell
# Read the current content, apply your correction, write it back
$r = Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard?repo_path=<repo_path>&filename=<file>" -Method GET
$fixed = $r.content -replace "<old text>", "<corrected text>"
$patchBody = @{ repo_path = "<repo_path>"; filename = "<file>"; content = $fixed } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard" -Method POST -Body $patchBody -ContentType "application/json"
```

Keep patches minimal — one targeted correction per patch. If you find yourself rewriting paragraphs,
that is a RETRY, not a PATCH.

---

## Batch compile check and test check

Run **both** after every `/run-batch` call in step 8, before starting the next batch. A compile check
alone is not enough — code that compiles but fails its tests is broken output.

**Run compile first.** If compile fails, do not run tests — fix compile errors first, then re-run both.
**Only after compile passes, run the test check.** If tests fail, RETRY the specific failing unit(s)
with the test failure output as context before starting the next batch.

Detect the project type once before step 7 begins:

```powershell
$isGradle  = Test-Path (Join-Path $repoPath "build.gradle")
$isMaven   = Test-Path (Join-Path $repoPath "pom.xml")
$isTsNode  = Test-Path (Join-Path $repoPath "tsconfig.json")
$isJsNode  = (Test-Path (Join-Path $repoPath "package.json")) -and -not $isTsNode
```

Then after each batch, dispatch a compile-check coder run:

```powershell
$compileInstruction = if ($isGradle) {
    "COMPILE CHECK ONLY — do not modify any files. Run: ./gradlew compileJava 2>&1. Report exit code and any error lines verbatim. If exit 0, output exactly: COMPILE OK"
} elseif ($isMaven) {
    "COMPILE CHECK ONLY — do not modify any files. Run: ./mvnw compile -q 2>&1. Report exit code and any error lines verbatim. If exit 0, output exactly: COMPILE OK"
} elseif ($isTsNode) {
    "COMPILE CHECK ONLY — do not modify any files. Run: npx tsc --noEmit 2>&1. Report exit code and any type errors verbatim. If exit 0, output exactly: COMPILE OK"
} elseif ($isJsNode) {
    "COMPILE CHECK ONLY — do not modify any files. Run: node --check \$(node -e 'console.log(require(""./package.json"").main||""index.js"")') 2>&1. If that fails, run: npx eslint . --ext .js --quiet 2>&1. Report exit code. If exit 0, output exactly: COMPILE OK"
} else {
    "COMPILE CHECK ONLY — do not modify any files. Read analysis.md or implementation.md to find the build/compile command for this project. Run it with the minimal flags needed to check compilation only (no tests). Report exit code and errors. If no compile step exists (pure interpreted/scripted project), output exactly: COMPILE OK — no compile step"
}

$compileBody = @{
    agent          = "coder"
    repo_path      = $repoPath
    instruction    = $compileInstruction
    resume_session = $false
} | ConvertTo-Json -Depth 5
$compileResult = Invoke-RestMethod -Uri "http://127.0.0.1:8765/run" -Method POST -Body $compileBody -ContentType "application/json" -TimeoutSec 300
```

Read the compile output:
- Contains errors → RETRY: re-run the specific failing unit(s) with compiler errors as context. Re-run compile before advancing.
- Contains `COMPILE OK` → run the test check:

```powershell
$testInstruction = if ($isGradle) {
    "TEST CHECK ONLY — do not modify any files. Run: ./gradlew test 2>&1. Report: X tests passed, Y failed, Z skipped. List every failing test class and its failure message verbatim. If all tests pass with 0 failures, output exactly: TEST OK"
} elseif ($isMaven) {
    "TEST CHECK ONLY — do not modify any files. Run: ./mvnw test 2>&1. Report: X tests passed, Y failed, Z skipped. List every failing test class and failure message verbatim. If all tests pass with 0 failures, output exactly: TEST OK"
} elseif ($isTsNode) {
    "TEST CHECK ONLY — do not modify any files. Run: npm test -- --passWithNoTests 2>&1. Report: X tests passed, Y failed. List any failing test names and their assertion errors verbatim. If all pass with 0 failures, output exactly: TEST OK"
} else {
    "TEST CHECK ONLY — do not modify any files. Find and run the full test suite from analysis.md or implementation.md. Report pass/fail counts and any failures verbatim. If all pass, output exactly: TEST OK"
}

$testBody = @{
    agent          = "coder"
    repo_path      = $repoPath
    instruction    = $testInstruction
    resume_session = $false
} | ConvertTo-Json -Depth 5
$testResult = Invoke-RestMethod -Uri "http://127.0.0.1:8765/run" -Method POST -Body $testBody -ContentType "application/json" -TimeoutSec 300
```

Read the test output:
- Contains `TEST OK` → advance to next batch
- Contains failures → RETRY: re-run each failing unit via `/run` with `resume_session: $true`, passing the exact failure messages as context. Re-run both compile and test checks before advancing. Do **not** start batch N+1 with failing tests in batch N.

---

## Pipeline

Run every step in this exact order — all 11 are mandatory. Never skip a step. Agents that are not
applicable to a given project (e.g., designer for a backend-only project) will self-detect and produce
a minimal output. After each step, apply the **per-step review protocol** before advancing.

**Model assignment** (for your awareness — the server handles routing automatically):
| Agent | Model | Notes |
|---|---|---|
| Architect | Claude Opus 4.8 | Extended thinking, 16 000 budget tokens |
| Planner | Claude Sonnet 4.6 | Extended thinking, 10 000 budget tokens |
| Designer, Reviewer, QA-Tester, Code-Reviewer, Orchestrator | DeepSeek v4 Pro | Thinking max effort |
| All others | DeepSeek v4 Pro | No extended thinking |

### 1 — Architect *(Claude Opus 4.8 — extended thinking)*

**If `$anthropicAvailable` — dispatch to server:**

```powershell
$body = @{
    agent       = "architect"
    repo_path   = $repoPath
    instruction = "Read task.md from the blackboard. If task.md references any documentation files
(e.g. a /docs folder, a spec, a design doc), read every one of them with read_file before designing —
do not rely on task.md alone. Explore the codebase with list_files and read_file. Write three files:
analysis.md (research, design decisions, constraints, stack/framework identification, dependencies that
must be added); requirements.md (numbered, individually testable requirements — specific enough that
a single test can pass or fail each one); and conventions.md (three sections required — Namespace
Declarations: exact package names, file path patterns, and class/file naming for every layer;
Architectural Decisions: every concern with two valid options resolved to one choice with the alternative
explicitly prohibited; Platform Constraints: Java/Node version, javax→jakarta migration, Mockito strict
stubbing default, TypeScript strict mode, deprecated APIs, any known framework gotchas)."
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8765/run" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 600
```

**If NOT `$anthropicAvailable` — you ARE the architect. Perform this role inline:**

1. Read `task.md` from the blackboard:
   ```powershell
   $taskMd = (Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard?repo_path=$repoPath&filename=task.md" -Method GET).content
   ```
2. Read every doc file referenced in `task.md` using your `Read` tool. Explore the codebase with `Glob` and `Read`.
3. Produce all three outputs at the quality level of a senior architect:
   - **`analysis.md`** — `# Analysis` heading; `## Overview`, `## Stack`, `## Constraints`, `## Design Decisions`, `## Dependencies` sections; verified Maven/npm coordinates for every external dep; ≥200 chars.
   - **`requirements.md`** — numbered `1.` through `N.`; each requirement specific enough that exactly one test can pass or fail it; ≥100 chars.
   - **`conventions.md`** — `## Namespace Declarations` (exact package/path/class naming per layer), `## Architectural Decisions` (every fork resolved, alternative prohibited), `## Platform Constraints` (versions, javax→jakarta, Mockito strict, `@WebMvcTest` security filter note, TypeScript strict, `## Test Dependencies` with verbatim coordinates); ≥100 chars.
4. Write each file to the blackboard and write the checklist:
   ```powershell
   $files = @{
       "analysis.md"          = "<your analysis.md content>"
       "requirements.md"      = "<your requirements.md content>"
       "conventions.md"       = "<your conventions.md content>"
       "checklist/architect.md" = "## Completion Checklist`n`n### Done criteria`n- [x] analysis.md written`n- [x] requirements.md written`n- [x] conventions.md written — all three sections`n- [x] No vague requirements`n- [x] Every requirement traces to task.md`n`n### What I did`n- Performed inline by orchestrator (no ANTHROPIC_API_KEY)"
   }
   foreach ($fn in $files.Keys) {
       $bb = @{ repo_path = $repoPath; filename = $fn; content = $files[$fn] } | ConvertTo-Json -Depth 5
       Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard" -Method POST -Body $bb -ContentType "application/json"
   }
   ```

**After (both paths):** read `checklist/architect.md` — if any `- [ ]` exists, redo that criterion.
Then read analysis.md, requirements.md, conventions.md. Confirm requirements are numbered and testable.
Confirm conventions.md has all three sections and no open-ended decisions. Apply per-step review protocol.

### 2 — Designer
Instruction: "Read analysis.md and requirements.md. If this project has a UI layer, write design-spec.md
with the full component hierarchy, design tokens (CSS custom properties), per-component state catalogue,
responsive breakpoints, and accessibility requirements. If this is a backend/API-only project, do NOT
skip the file — write design-spec.md stating 'no UI layer' and instead document the API contract the
consumer will rely on: every endpoint's request/response DTO shape with exact field names and types,
status codes, and any state machines or encoding formats (so the test-generator and coder have one
authoritative contract)."
After: read `checklist/designer.md` first — if any `- [ ]` exists, RETRY. Then read design-spec.md.
For a UI project, confirm every UI requirement maps to a component; for a backend project, confirm every
endpoint has a documented request/response shape.

### 3 — Planner *(Claude Sonnet 4.6 — extended thinking)*

**If `$anthropicAvailable` — dispatch to server:**

```powershell
$body = @{
    agent       = "planner"
    repo_path   = $repoPath
    instruction = "Read conventions.md first. All package names, file path patterns, and class naming in
work-plan.md must be copied verbatim from the Namespace Declarations section of conventions.md — do not
infer or invent package names from the project name or task description. Then read analysis.md,
requirements.md, and design-spec.md. Identify all work units, build a dependency graph, and write
work-plan.md with TICKET-NNN numbered parallel batches."
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8765/run" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 600
```

**If NOT `$anthropicAvailable` — you ARE the planner. Perform this role inline:**

1. Read from the blackboard: `conventions.md`, `analysis.md`, `requirements.md`, `design-spec.md`.
2. Produce `work-plan.md`:
   - **Batch 1** always: CI workflow (`TICKET-001`) + contract/scaffold files — no business logic.
   - **Batch 2+**: independent work units per requirement, one file or one logical unit per ticket, dependency order enforced (nothing in batch N depends on batch N or later).
   - Every ticket uses this format:
     ```
     ### TICKET-NNN — <title>
     **Req:** <requirement numbers>
     **File:** `<exact path from conventions.md Namespace Declarations>`
     **Instruction:** <specific implementation instruction including method signatures from contracts>
     ```
   - Package names and file paths copied verbatim from `conventions.md → ## Namespace Declarations` — do not invent.
3. Write to the blackboard:
   ```powershell
   $files = @{
       "work-plan.md"         = "<your work-plan.md content>"
       "checklist/planner.md" = "## Completion Checklist`n`n### Done criteria`n- [x] work-plan.md written with TICKET-NNN format`n- [x] Batch 1 is contracts + CI only`n- [x] No batch-ordering violations`n- [x] Every requirement maps to ≥1 ticket`n- [x] All paths copied verbatim from conventions.md`n`n### What I did`n- Performed inline by orchestrator (no ANTHROPIC_API_KEY)"
   }
   foreach ($fn in $files.Keys) {
       $bb = @{ repo_path = $repoPath; filename = $fn; content = $files[$fn] } | ConvertTo-Json -Depth 5
       Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard" -Method POST -Body $bb -ContentType "application/json"
   }
   ```

**After (both paths) — fetch and parse the work plan:**
```powershell
$wp = Invoke-RestMethod -Uri "http://127.0.0.1:8765/blackboard?repo_path=<repo_path>&filename=work-plan.md" -Method GET
$workPlan = $wp.content
```

The work plan uses a ticket-based format. Parse it as follows:
- `## Batch N` — top-level batch sections; one `/run-batch` call per batch in step 8
- `### TICKET-NNN — <title>` — individual work unit within the batch
- `**Instruction:**` — the verbatim text to pass to the Coder (or Scaffolder for Batch 1)

Extract all `TICKET-NNN` IDs from the full work plan into a list — you will pass these to the commit
agent in step 11 so they appear in the `Closes:` trailer.

```powershell
# Extract all ticket IDs from work-plan.md
$ticketIds = [regex]::Matches($workPlan, 'TICKET-\d+') | ForEach-Object { $_.Value } | Sort-Object -Unique
$ticketList = $ticketIds -join ', '
```

Read `checklist/planner.md` first — if any `- [ ]` exists, RETRY. Then sanity-check the plan:
Batch 1 must be contracts + test env + CI only; no work unit may depend on another unit in the same
or a later batch; every requirement maps to at least one ticket. If the plan violates batch ordering,
re-run the planner with the specific violation as context.

### 4 — Scaffolder

Instruction: "Read conventions.md, work-plan.md, analysis.md, and design-spec.md from the blackboard.
Implement Batch 1 ONLY — contract files (interfaces, DTOs, request/response models, entities, type
definitions, enums, exception declarations, repository interfaces, schema files) AND test environment
setup (application-test.yml with H2 in-memory DB for Spring; jest.config.*/vitest.config.ts + setup.ts
for JS/TS projects). Do NOT implement any business logic or method bodies. Follow namespace declarations
from conventions.md exactly — do not infer package names. After writing all files, run the compile check
and write contracts.md with all four structured sections: Interfaces (fully-qualified names + method
signatures), Exceptions (qualified name + checked/unchecked + package), Data Shapes (field list per
type), Inter-layer Conventions (layer boundary rules, error body shapes, pagination format)."

After: read `checklist/scaffolder.md` first — if any `- [ ]` exists, RETRY. Then read `contracts.md`
and confirm all four sections exist and compile exited 0. Confirm test env files are listed. PATCH a
single missing entry inline; RETRY if compilation failed, business logic crept in, or test environment
is missing entirely. Do not advance to tester until both contracts compile AND test environment exists.

### 5 — Tester (max 3 retries)
First run: "Read analysis.md, requirements.md, and design-spec.md (if it exists). Write a comprehensive
test plan to test-plan.md. Every numbered requirement in requirements.md must map to at least one test.
For each test, specify four things explicitly: the test class name, the test method name, the input/
setup, the action, and the expected outcome (exact status code / value / error). Cover happy path, edge
cases, boundary values, error handling, and auth/authz. Frontend tests use Playwright against real DOM
elements; backend tests cover the full request/response cycle. Include a dedicated E2E section for
multi-layer projects. Do not write a test that asserts something trivially true — every test must be
able to fail."
On retry: "Read test-review.md. Address every gap the reviewer raised — do not just acknowledge them.
Rewrite test-plan.md in full."
After each tester run: read `checklist/tester.md` — if any `- [ ]` exists, RETRY before sending to reviewer.

### 6 — Reviewer (gate)
Instruction: "Read analysis.md, requirements.md, design-spec.md, and test-plan.md. Verify every
requirement maps to a test and every test has input/action/outcome. Write test-review.md.
End with exactly: VERDICT: PASS or VERDICT: FAIL + gaps."
After: read `checklist/reviewer.md` first — if any `- [ ]` exists, RETRY. Then read the tail of
test-review.md and act only on the explicit `VERDICT: PASS` / `VERDICT: FAIL` token.
- PASS → advance to test-generator
- FAIL → loop back to tester (resume_session: true) with the listed gaps as context. Re-run the reviewer
  after. If tester has already run 3 times and still FAILs → halt and report the outstanding gaps.

### 7 — Test-Generator
Instruction: "Read conventions.md (Platform Constraints section for test framework defaults and
Namespace Declarations for correct import paths). Read contracts.md (authoritative interface
signatures, exception types with packages, data shapes — use these for imports and assertions).
Read test-plan.md, test-review.md, and design-spec.md (for component names/selectors).
Read existing test files for framework conventions and test environment setup files (application-test.yml,
jest.config, vitest.config) to confirm the test environment is ready before writing tests.
Implement the approved test plan as real test code with genuine assertions.

CRITICAL — do not write placeholder or stub assertions. Specifically banned: `assertTrue(true)`,
`assert True`, `expect(true).toBe(true)`, empty test bodies, `TODO` comments in place of assertions,
or any test that passes without exercising real behavior. Every test must assert the actual expected
outcome from test-plan.md (exact status code, value, thrown exception, or DOM state).

The test code WILL fail to compile or fail at runtime because the production classes it references do
not exist yet. That is the correct and expected state — do NOT write production code, do NOT create
stub production classes, and do NOT weaken assertions to make tests pass. Compile errors here are a
sign you did it right.

Concrete assertion patterns:
- HTTP/controller tests: `mockMvc.perform(...).andExpect(status().isXxx()).andExpect(jsonPath(...).value(...))`
  (or the framework's equivalent — supertest `.expect(401)`, etc.)
- Exception/unit tests: `assertThrows(SomeException.class, () -> ...)`, `assertEquals(expected, actual)`,
  `assertThat(...)` — never a tautology.
- Filter/middleware/security tests: assert the real resulting state (e.g. SecurityContext populated,
  401 body shape), not that the code merely ran.
- Encoding/codec tests: decode the produced value and assert its internal structure.

Test class names and method names must match test-plan.md exactly."
After: read `checklist/test-generator.md` first — if any `- [ ]` exists (including the "no stub assertions"
criterion), RETRY pointing at the specific offending files. Then spot-check 2–3 generated test files to
confirm real assertions exist before advancing to the coder.

### 8 — Coder

Use the `$workPlan` content you read in step 3. The scaffolder has already implemented Batch 1.
**Start from Batch 2.** For each `## Batch N` section from Batch 2 onwards, build a tasks array
from its work units and POST to `/run-batch`. Units within the same batch are independent and run
in parallel; do not start batch N+1 until batch N's `/run-batch` call returns.

**Before constructing the tasks array**, read `conventions.md` and `contracts.md` from the blackboard.
Append a brief extract of both to each coder unit's instruction so the agent does not have to re-derive
namespace or re-read interfaces:

```
Preamble to prepend to every coder unit instruction:
"Read conventions.md and contracts.md from the blackboard before writing any code.
Follow the Namespace Declarations and Architectural Decisions exactly — do not infer or invent package
names, file paths, or class naming patterns; copy them verbatim from conventions.md.
The interface you are implementing is listed in contracts.md → Interfaces section.
After writing your files, append your public method signatures to contracts.md.
CRITICAL: Do not move, delete, rename, or overwrite any existing test files or test directories
(src/test/, __tests__/, *.test.*, *.spec.*). Your scope is production source files only.
Test files are read-only from your perspective — treat any change to them as a scope violation."
```

For each batch in order:

```powershell
# One call per batch — do NOT start the next batch until this one returns
# Parse ticket instructions from the batch section: extract each ### TICKET-NNN block's **Instruction:** value
$body = @{
    repo_path = "<repo_path>"
    tasks = @(
        @{ agent = "coder"; instruction = "<preamble above> + TICKET-003: <**Instruction:** value from work-plan.md>" },
        @{ agent = "coder"; instruction = "<preamble above> + TICKET-004: <**Instruction:** value from work-plan.md>" }
        # one entry per ### TICKET-NNN in this batch
    )
} | ConvertTo-Json -Depth 5
$batchResult = Invoke-RestMethod -Uri "http://127.0.0.1:8765/run-batch" -Method POST -Body $body -ContentType "application/json" -TimeoutSec 900
# $batchResult.wall_time  — confirms parallelism (≈ slowest task, not sum)
# $batchResult.results    — check every entry has status "done"
```

If the work plan has only one work unit total, use `/run` instead — no batch needed.

After each batch:
1. Read `checklist/coder.md` (or per-instance suffixed file if parallel) — if any `- [ ]` exists,
   RETRY that specific unit before continuing.
2. Verify every entry in `$batchResult.results` has `status: "done"` — if any failed, re-run the
   failing unit(s) via `/run` with `resume_session: $true` before continuing.
3. If the client HTTP call times out, do NOT assume the batch failed — `list_files` the repo to see
   which units' files were written, then re-dispatch only the missing units.
4. **Run the batch compile check then the batch test check** (see `## Batch compile check and test check`
   above). Do not start the next batch until both return `COMPILE OK` and `TEST OK`. Fix compile errors
   first, then test failures, then re-run both checks before advancing.

After all batches: run the project's full build/test command and confirm it exits 0 with all tests passing.
Use the actual command for the stack (e.g. `./gradlew test`, `npm test`, `pytest`) — find it in
analysis.md or the build config; do not guess. Capture the output; if tests fail, read the failures.

On coder failure (a unit returns non-"done", or the build/tests fail because of a unit's code): re-run
that specific unit with `/run` and `resume_session: true`, passing the exact error/failure output as
context. Limit: 5 re-runs per individual unit. If a unit still fails after 5 re-runs, halt and report
the failing unit and its error to the user — do not silently advance with a broken build.

### 9 — QA-Tester (max 3 retries per coder cycle)
Instruction: "Run the full test suite. Probe the system adversarially: try invalid inputs, boundary
values, auth bypass attempts, injection vectors. Write qa-report.md.
End with exactly: VERDICT: PASS or VERDICT: FAIL + specific failures."
After: read `checklist/qa-tester.md` first — if any `- [ ]` exists, RETRY. Then read the tail of
qa-report.md and act only on the explicit `VERDICT:` token.
- PASS → advance to code-reviewer
- FAIL → re-run coder with `resume_session: true`, tell it to read qa-report.md and pass the specific
  failures as context. Re-run qa-tester after. Max 3 coder retries; if still FAIL → halt and report.

### 10 — Code Reviewer (gate)
Instruction: "Read requirements.md, analysis.md, implementation.md, and all modified source files.
Review for correctness, edge cases, code quality, security. Write code-review.md.
End with exactly: VERDICT: PASS or VERDICT: FAIL + specific issues."
After: read `checklist/code-reviewer.md` first — if any `- [ ]` exists, RETRY. Then read the tail of
code-review.md and act only on the explicit `VERDICT:` token.
- PASS → advance to commit
- FAIL → re-run coder with `resume_session: true`, tell it to read code-review.md and pass the specific
  issues as context. Re-run qa-tester and code-reviewer after. Max 3 coder retries; if still FAIL → halt
  and report the outstanding issues.

### 11 — Commit
Instruction: "Read task.md, implementation.md, and work-plan.md from the blackboard. Extract every
TICKET-NNN ID from work-plan.md. Stage all modified files (exclude .blackboard/, logs/, generated
artifacts). Form a commit message: one subject line under 72 chars describing what was built, then a
blank line, then `Closes: <comma-separated list of all ticket IDs>`. Write the full commit hash and
message to commit.md.

Tickets closed by this commit: <paste $ticketList here>"
After: read `checklist/commit.md` — if any `- [ ]` exists, RETRY. Confirm the commit hash and `Closes:` trailer are present in commit.md.

### 12 — Documentation
Instruction: "Read the full blackboard and all modified files. Write docs.md (what was built, why,
how to use it, key decisions, limitations) and pr-description.md (clean PR description ready to paste)."
After: read `checklist/documentation.md` — if any `- [ ]` exists, RETRY.

---

## Rework requests

After any agent runs, check if its result contains rework requests. If so:
1. Re-run the target agent with the rework reason as explicit context
2. Re-run the agent that filed the rework
3. Apply the same retry limits as the standard loops

---

## Loop escalation

If an agent has been called its maximum number of times without resolving, do not retry it again.
Accept the best available blackboard output and move forward, or stop and ask the user what to do.

---

## Completion report

Output a strictly-formatted orchestrator checklist, then a summary:

```
## Orchestrator Completion Checklist

### Pipeline steps
- [x] 1 — Architect: analysis.md ✓ | requirements.md ✓ | conventions.md ✓ | checklist ✓
- [x] 2 — Designer: design-spec.md ✓ | checklist ✓
- [x] 3 — Planner: work-plan.md ✓ (<N> tickets across <N> batches) | checklist ✓
- [x] 4 — Scaffolder: contracts.md ✓ | ci.yml ✓ | test env ✓ | checklist ✓
- [x] 5 — Tester: test-plan.md ✓ (<N> tests) | checklist ✓
- [x] 6 — Reviewer: VERDICT: PASS | checklist ✓
- [x] 7 — Test-generator: <N> test files | real assertions ✓ | checklist ✓
- [x] 8 — Coder: build ✓ | tests ✓ (<N> passed) | <N> batches | checklist ✓
- [x] 9 — QA-tester: VERDICT: PASS | checklist ✓
- [x] 10 — Code-reviewer: VERDICT: PASS | checklist ✓
- [x] 11 — Commit: <hash> | Closes: <N> tickets | checklist ✓
- [x] 12 — Documentation: docs.md ✓ | pr-description.md ✓ | checklist ✓

### What I (orchestrator) did
- <retry count per agent, e.g. "Tester retried once — missing auth tests">
- <patches applied inline to blackboard files>
- <any escalation or halt conditions hit>
- <total wall time estimate>
```

Then tell the user: what was built, architect's key decisions, which agents retried and why, where docs live.

## Halt conditions

Stop and explain clearly if: server unreachable, agent hits max_iterations, retry gate exceeded,
or agent writes nothing to its expected blackboard file.
