import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# provider       — "deepseek" or "anthropic"
# thinking       — enable extended reasoning for this agent
# reasoning_effort — DeepSeek only: "high" | "max"
# budget_tokens  — Anthropic only: thinking token budget (output cap = budget + 8192)
MODELS = {
    "orchestrator":   {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "architect":      {"model": "claude-opus-4-8",   "provider": "anthropic",  "thinking": True,  "budget_tokens": 16000},
    "reconciler":     {"model": "claude-opus-4-8",   "provider": "anthropic",  "thinking": True,  "budget_tokens": 16000},
    "designer":       {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "planner":        {"model": "claude-sonnet-4-6", "provider": "anthropic",  "thinking": True,  "budget_tokens": 10000},
    "tester":         {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": False},
    "reviewer":       {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "test-generator": {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": False},
    "scaffolder":     {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": False},
    "coder":          {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "qa-tester":      {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "code-reviewer":  {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": True,  "reasoning_effort": "max"},
    "commit":         {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": False},
    "documentation":  {"model": "deepseek-v4-pro",  "provider": "deepseek",   "thinking": False},
    "consultant":     {"model": "claude-sonnet-4-6", "provider": "anthropic",  "thinking": True, "budget_tokens": 8000},
    "judge":          {"model": "claude-opus-4-8",   "provider": "anthropic",  "thinking": True, "budget_tokens": 16000},
}

MAX_TOOL_ITERATIONS = 30  # fallback when agent not in AGENT_TOOL_ITERATIONS
BLACKBOARD_DIR      = ".blackboard"

# Max tool calls an agent may make inside its own agentic loop.
# Code-touching agents and testers get a high cap; read-only agents stay lean.
AGENT_TOOL_ITERATIONS: dict[str, int] = {
    "orchestrator":   50,
    "architect":      40,
    "designer":       30,
    "planner":        25,
    "tester":         60,
    "reviewer":       20,
    "test-generator": 80,
    "scaffolder":     60,
    "coder":          200,
    "qa-tester":      80,
    "reconciler":     40,
    "consultant":     30,
    "code-reviewer":  30,
    "commit":         20,
    "documentation":  20,
    "judge":          15,
}

# Max times the orchestrator may invoke the same agent across the whole pipeline run.
# Prevents rework loops from spinning forever; triggers escalation when exceeded.
AGENT_MAX_CALLS: dict[str, int] = {
    "orchestrator":   1,
    "architect":      2,
    "designer":       2,
    "planner":        2,
    "tester":         3,
    "reviewer":       3,
    "test-generator": 2,
    "scaffolder":     2,
    "coder":          5,
    "qa-tester":      3,
    "reconciler":     5,
    "code-reviewer":  3,
    "commit":         2,
    "documentation":  2,
}

# How many instances of each agent to run in parallel per invocation.
# Each instance writes to suffixed blackboard files (e.g. test-plan-2.md, NOT test-plan.md-2);
# the orchestrator reconciles them into the canonical file afterwards.
# coder and test-generator must stay at 1 — they write directly to the codebase.
AGENT_CONCURRENCY: dict[str, int] = {
    "architect":      1,
    "designer":       1,  # single design spec — parallel versions would conflict
    "planner":        1,  # single work plan — parallel versions would conflict
    "tester":         1,
    "reviewer":       1,
    "test-generator": 1,  # writes to codebase — do not increase
    "scaffolder":     1,  # writes to codebase — do not increase
    "coder":          1,  # writes to codebase — do not increase
    "qa-tester":      1,
    "reconciler":     1,
    "code-reviewer":  1,
    "commit":         1,
    "documentation":  1,
}

# Per-agent capability declarations.
# tools      — exact set of tool names exposed to the agent via the API schema.
#              The LLM can only call tools it sees in the schema; this is the primary gate.
# write_deny — path glob patterns (supports prefix/** and **/suffix) for write_file.
#              Any write whose path matches a deny pattern is blocked at the executor level.
#              Agents without write_file in their tools list are implicitly deny-all for writes.
# Agents not listed here (e.g. orchestrator) fall back to the legacy READ_ONLY / CONTRACT_ONLY flags.
AGENT_CAPABILITIES: dict[str, dict] = {
    # ── read-only agents: explore codebase, write only to blackboard ─────────
    "architect": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "designer": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "planner": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "tester": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "reviewer": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "code-reviewer": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "documentation": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },

    # ── scaffolder: writes contracts, CI, test env; runs compile check ───────
    "scaffolder": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard",
                       "write_file", "run_command"},
        "write_deny": [
            "src/test/java/**",    # no test source (only test/resources allowed)
            "src/test/kotlin/**",
            "__tests__/**",
            "**/*.test.ts", "**/*.test.js", "**/*.test.tsx",
            "**/*.spec.ts", "**/*.spec.js", "**/*.spec.tsx",
        ],
    },

    # ── test-generator: writes test files only; read_contract_file blocks impls ──
    "test-generator": {
        "tools":      {"list_files", "read_contract_file", "read_blackboard", "write_blackboard",
                       "write_file"},
        "write_deny": [
            "src/main/**",         # no production source
            "src/main/java/**",
            "src/main/kotlin/**",
            "src/main/resources/**",
        ],
    },

    # ── coder: writes production source; cannot touch test files ─────────────
    "coder": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard",
                       "write_file", "run_command"},
        "write_deny": [
            "src/test/**",
            "__tests__/**",
            "test/**",
            "tests/**",
            "qa-tests/**",
            "**/*.test.ts", "**/*.test.js", "**/*.test.tsx",
            "**/*.spec.ts", "**/*.spec.js", "**/*.spec.tsx",
        ],
    },

    # ── qa-tester: runs tests + attack commands; can add qa-test files ───────
    "qa-tester": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard",
                       "write_file", "run_command"},
        "write_deny": [
            "src/main/**",         # cannot modify production source
        ],
    },

    # ── commit: git commands only; no codebase writes ────────────────────────
    "commit": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard",
                       "run_command"},
        "write_deny": [],          # write_file not in tools, so deny patterns are moot
    },

    # ── reconciler: reads parallel drafts, writes canonical blackboard files ──
    "reconciler": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
    "consultant": {
        "tools":      {"list_files", "read_file", "read_blackboard"},
        "write_deny": [],
    },
    "judge": {
        "tools":      {"list_files", "read_file", "read_blackboard", "write_blackboard"},
        "write_deny": [],
    },
}

# Canonical blackboard output files produced by each agent.
# Used by the reconciler to know which draft files to merge.
AGENT_OUTPUTS: dict[str, list[str]] = {
    "architect":      ["analysis.md", "requirements.md", "conventions.md"],
    "designer":       ["design-spec.md"],
    "planner":        ["work-plan.md"],
    "tester":         ["test-plan.md"],
    "reviewer":       ["test-review.md"],
    "test-generator": [],
    "scaffolder":     ["contracts.md"],
    "coder":          ["implementation.md"],
    "qa-tester":      ["qa-report.md"],
    "code-reviewer":  ["code-review.md"],
    "commit":         ["commit.md"],
    "documentation":  ["docs.md", "pr-description.md"],
    "reconciler":     [],
}
