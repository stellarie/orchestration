import json
import logging
from agents.orchestrator import OrchestratorAgent, ORCHESTRATOR_TOOLS

logger = logging.getLogger(__name__)

# Maps user-friendly gate names → canonical agent names
GATE_ALIASES: dict[str, str] = {
    "analysis":       "architect",
    "architect":      "architect",
    "design":         "designer",
    "designer":       "designer",
    "plan":           "planner",
    "planner":        "planner",
    "test-plan":      "tester",
    "tester":         "tester",
    "review":         "reviewer",
    "reviewer":       "reviewer",
    "scaffold":       "scaffolder",
    "scaffolder":     "scaffolder",
    "tests":          "test-generator",
    "test-generator": "test-generator",
    "coder":          "coder",
    "qa":             "qa-tester",
    "qa-tester":      "qa-tester",
    "code-review":    "code-reviewer",
    "code-reviewer":  "code-reviewer",
    "commit":         "commit",
    "docs":           "documentation",
    "documentation":  "documentation",
}

# Blackboard files to surface at each gate
GATE_OUTPUTS: dict[str, list[str]] = {
    "architect":      ["analysis.md", "requirements.md", "conventions.md", "checklist/architect.md"],
    "designer":       ["design-spec.md", "checklist/designer.md"],
    "planner":        ["work-plan.md", "checklist/planner.md"],
    "tester":         ["test-plan.md", "checklist/tester.md"],
    "reviewer":       ["test-review.md", "checklist/reviewer.md"],
    "scaffolder":     ["contracts.md", "checklist/scaffolder.md"],
    "test-generator": ["checklist/test-generator.md"],
    "coder":          ["implementation.md", "checklist/coder.md"],
    "qa-tester":      ["qa-report.md", "checklist/qa-tester.md"],
    "code-reviewer":  ["code-review.md", "checklist/code-reviewer.md"],
    "commit":         ["commit.md", "checklist/commit.md"],
    "documentation":  ["docs.md", "pr-description.md", "checklist/documentation.md"],
}

RAISE_TO_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "raise_to_user",
        "description": (
            "Raise a question, conflict, or ambiguity to the human operator. "
            "Use when you encounter conflicting requirements, unclear scope, or need "
            "human judgement to proceed. Blocks until the user responds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question or conflict to resolve",
                },
                "context": {
                    "type": "string",
                    "description": "Brief context to help the user understand and decide",
                },
            },
            "required": ["question"],
        },
    },
}

DELEGATED_TOOLS = ORCHESTRATOR_TOOLS + [RAISE_TO_USER_SCHEMA]

_SEP  = "═" * 60
_DASH = "─" * 60


class DelegatedOrchestratorAgent(OrchestratorAgent):
    NAME = "orchestrator"

    def __init__(self, repo_path: str, gates: list[str] | None = None):
        super().__init__(repo_path)
        raw = gates or []
        self.active_gates: set[str] = {GATE_ALIASES.get(g, g) for g in raw}
        if self.active_gates:
            logger.info("[delegated] active gates: %s", self.active_gates)

    # ── override points ──────────────────────────────────────────────────────

    def _get_tools(self) -> list:
        return DELEGATED_TOOLS

    def _dispatch_tool(self, name: str, args: dict) -> str:
        if name == "raise_to_user":
            return self._handle_raise_to_user(args)
        return super()._dispatch_tool(name, args)

    # ── gate-aware + escalation-aware agent runner ───────────────────────────

    def _run_pipeline_agent(self, args: dict) -> str:
        result_json = super()._run_pipeline_agent(args)
        result_data = json.loads(result_json)
        agent_name  = args["agent"]

        # Auto-raise escalated agents to the human
        if result_data.get("status") == "escalated":
            user_response = self._handle_raise_to_user({
                "question": (
                    f"`{agent_name}` is stuck in a loop — called "
                    f"{result_data.get('iterations', '?')} time(s) without resolving. "
                    f"How should we proceed?"
                ),
                "context": result_data["output"],
            })
            result_data["user_decision"] = user_response
            return json.dumps(result_data)

        if agent_name in self.active_gates:
            user_feedback = self._pause_at_gate(agent_name)
            if user_feedback:
                result_data["user_feedback"] = user_feedback
                return json.dumps(result_data)

        return result_json

    # ── human interaction ────────────────────────────────────────────────────

    def _handle_raise_to_user(self, args: dict) -> str:
        question = args["question"]
        context  = args.get("context", "")

        print(f"\n{_SEP}")
        print("  ORCHESTRATOR QUESTION")
        print(_SEP)
        if context:
            print(f"\nContext:\n{context}\n")
        print(f"Question:\n{question}")
        print(_DASH)
        response = input("Your answer: ").strip()
        print(f"{_SEP}\n")

        logger.info("[delegated] user answered raise_to_user: %s", response[:120])
        return response or "(no response)"

    def _pause_at_gate(self, agent_name: str) -> str:
        filenames = GATE_OUTPUTS.get(agent_name, [])

        print(f"\n{_SEP}")
        print(f"  GATE: {agent_name} complete")
        print(_SEP)

        for filename in filenames:
            content = self.bb.read(filename)
            if not content.startswith("["):
                print(f"\n── {filename} {'─' * max(0, 54 - len(filename))}")
                preview = content[:2000]
                print(preview)
                if len(content) > 2000:
                    print(f"\n  ... [{len(content) - 2000} more chars, see .blackboard/{filename}]")

        print(f"\n{_DASH}")
        response = input("Press Enter to continue, or type feedback: ").strip()
        print(f"{_SEP}\n")

        if response:
            logger.info("[delegated] user feedback at gate '%s': %s", agent_name, response[:120])
        return response
