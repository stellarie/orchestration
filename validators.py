import logging
from blackboard import BlackBoard

logger = logging.getLogger(__name__)

# Per-agent: which blackboard files are expected, their minimum meaningful length,
# and which patterns must appear somewhere in the content.
_CHECKLIST = {"min_len": 50, "required": ["### Done criteria", "### What I did", "- [x]"]}

_CHECKS: dict[str, dict[str, dict]] = {
    "architect": {
        "analysis.md":           {"min_len": 200, "required": ["#", "## Dependencies"]},
        "requirements.md":       {"min_len": 100, "required": ["1."]},
        "conventions.md":        {"min_len": 100, "required": ["## Namespace", "## Architectural", "## Platform"]},
        "checklist/architect.md": _CHECKLIST,
    },
    "designer": {
        "design-spec.md":         {"min_len": 300, "required": ["##"]},
        "checklist/designer.md":  _CHECKLIST,
    },
    "planner": {
        "work-plan.md":          {"min_len": 100, "required": ["## Batch", "TICKET-"]},
        "checklist/planner.md":  _CHECKLIST,
    },
    "scaffolder": {
        "contracts.md":           {"min_len": 100, "required": ["### Interfaces", "### Exceptions", "### Data Shapes"]},
        "checklist/scaffolder.md": _CHECKLIST,
    },
    "tester": {
        "test-plan.md":          {"min_len": 200, "required": ["#"]},
        "checklist/tester.md":   _CHECKLIST,
    },
    "reviewer": {
        "test-review.md":        {"min_len": 50,  "required": ["VERDICT:"]},
        "checklist/reviewer.md": _CHECKLIST,
    },
    "test-generator": {
        "checklist/test-generator.md": _CHECKLIST,
    },
    "coder": {
        "implementation.md":    {"min_len": 20,  "required": []},
        "checklist/coder.md":   _CHECKLIST,
    },
    "qa-tester": {
        "qa-report.md":          {"min_len": 50,  "required": ["VERDICT:"]},
        "checklist/qa-tester.md": _CHECKLIST,
    },
    "code-reviewer": {
        "code-review.md":          {"min_len": 50,  "required": ["VERDICT:"]},
        "checklist/code-reviewer.md": _CHECKLIST,
    },
    "commit": {
        "commit.md":             {"min_len": 10,  "required": []},
        "checklist/commit.md":   _CHECKLIST,
    },
    "documentation": {
        "docs.md":                   {"min_len": 200, "required": ["#"]},
        "pr-description.md":         {"min_len": 50,  "required": []},
        "checklist/documentation.md": _CHECKLIST,
    },
}


def _has_unclosed_fence(text: str) -> bool:
    """Odd number of ``` delimiters means a code block was never closed."""
    return text.count("```") % 2 != 0


def validate_agent_output(agent_name: str, bb: BlackBoard) -> tuple[bool, list[str]]:
    """
    Validate that an agent's expected blackboard files exist, are non-trivially
    short, contain required structural markers, and have no unclosed code fences.

    Returns (passed, issues) where issues is a list of human-readable failure
    descriptions. An empty issues list means all checks passed.
    """
    checks = _CHECKS.get(agent_name)
    if not checks:
        return True, []  # test-generator etc. — no blackboard output to validate

    issues: list[str] = []

    for filename, spec in checks.items():
        content = bb.read(filename)

        if content.startswith("["):
            issues.append(f"{filename}: file not written")
            continue

        body = content.strip()

        if len(body) < spec["min_len"]:
            issues.append(
                f"{filename}: too short ({len(body)} chars, expected >={spec['min_len']})"
            )

        for pattern in spec.get("required", []):
            if pattern not in content:
                issues.append(f"{filename}: missing required marker {pattern!r}")

        if _has_unclosed_fence(content):
            issues.append(f"{filename}: unclosed code fence (odd number of ```)")

        if filename.startswith("checklist/") and "- [ ]" in content:
            issues.append(f"{filename}: contains unchecked item(s) — agent self-reported incompletion")

    passed = len(issues) == 0
    if passed:
        logger.info("[validator] %s — OK", agent_name)
    else:
        logger.warning("[validator] %s — FAILED: %s", agent_name, issues)

    return passed, issues
