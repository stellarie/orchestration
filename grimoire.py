"""
Grimoire loader — injects reference library content into agent system prompts.

Per AGENTS.md / CLAUDE.md load rules:
  - Always load: primers/agent-compendium.md
  - Conditionally load: at most one additional grimoire per agent, matched by domain
  - Never load: human-craft/* (human-only documents)
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

GRIMOIRE_ROOT = Path.home() / "grimoire"

# Per-agent conditional grimoire (relative to GRIMOIRE_ROOT).
# None = compendium only. "coder" is None because it is stack-detected at runtime.
_AGENT_GRIMOIRE: dict[str, str | None] = {
    "orchestrator":   None,
    "architect":      "universal/systems-design-grimoire.md",
    "designer":       "universal/frontend-design.md",
    "reconciler":     None,
    "consultant":     None,
    "planner":        "universal/systems-design-grimoire.md",
    "tester":         "universal/testing-grimoire.md",
    "reviewer":       "universal/testing-grimoire.md",
    "test-generator": "universal/testing-grimoire.md",
    "scaffolder":     "universal/systems-design-grimoire.md",
    "coder":          None,   # detected from instruction
    "qa-tester":      "universal/testing-grimoire.md",
    "code-reviewer":  "universal/antipatterns-atlas.md",
    "commit":         None,
    "documentation":  "universal/documentation-grimoire.md",
}

# Stack signals → grimoire path for the coder
_STACK_SIGNALS: list[tuple[list[str], str]] = [
    (
        [".java", "spring", "gradle", "maven", "jakarta", "hibernate", "jpa"],
        "stack/THE_FORBIDDEN_SPELLBOOK.md",
    ),
    (
        [".ts", ".tsx", ".jsx", "react", "typescript", "node", "express", "next"],
        "stack/grimoire-of-the-cursed-guild.md",
    ),
    (
        [".py", "django", "fastapi", "flask", "sqlalchemy"],
        None,   # no Python grimoire yet — compendium only
    ),
]


def _detect_coder_grimoire(instruction: str) -> str | None:
    lowered = instruction.lower()
    for signals, path in _STACK_SIGNALS:
        if any(s in lowered for s in signals):
            return path
    return None


def _read(relative_path: str) -> str | None:
    full = GRIMOIRE_ROOT / relative_path
    if not full.exists():
        logger.warning("[grimoire] file not found: %s", full)
        return None
    try:
        return full.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("[grimoire] could not read %s: %s", full, exc)
        return None


def load_for(agent_name: str, instruction: str = "") -> str:
    """
    Return grimoire content to inject into the agent's system prompt.
    Empty string if the grimoire root doesn't exist or nothing is applicable.
    """
    if not GRIMOIRE_ROOT.exists():
        logger.debug("[grimoire] root not found at %s — skipping", GRIMOIRE_ROOT)
        return ""

    parts: list[str] = []

    # Always load: compendium
    compendium = _read("primers/agent-compendium.md")
    if compendium:
        parts.append(f"## Grimoire — Agent Compendium\n\n{compendium}")
        logger.debug("[grimoire] loaded compendium for %s", agent_name)

    # Conditional: one domain grimoire
    conditional = _AGENT_GRIMOIRE.get(agent_name)
    if conditional is None and agent_name == "coder":
        conditional = _detect_coder_grimoire(instruction)
        if conditional:
            logger.debug("[grimoire] coder stack-detected: %s", conditional)

    if conditional:
        content = _read(conditional)
        if content:
            name = Path(conditional).stem.replace("-", " ").replace("_", " ")
            parts.append(f"## Grimoire — {name}\n\n{content}")
            logger.debug("[grimoire] loaded '%s' for %s", conditional, agent_name)

    if not parts:
        return ""

    header = "# Reference Library\n\nThe following grimoires are loaded for this run. Consult them when making design decisions, choosing patterns, or reviewing code quality. Do not quote them verbatim to the user — apply the wisdom.\n\n---\n\n"
    return header + "\n\n---\n\n".join(parts)
