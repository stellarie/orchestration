"""
Unified orchestration entry point.

Usage:
    python orchestrate.py --mode auto   --repo <path> "task"
    python orchestrate.py --mode delegated --gates analysis,coder,review --repo <path> "task"
"""
import sys
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from logging_config import setup_logging
setup_logging()

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Run the agent pipeline in auto or delegated mode.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "delegated"],
        default="auto",
        help="auto = fully autonomous; delegated = pauses at gates and can raise questions",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the target repository (default: current directory)",
    )
    parser.add_argument(
        "--gates",
        default="",
        help=(
            "Comma-separated gate names where execution pauses for your review "
            "(delegated mode only). At each gate the relevant blackboard output is "
            "printed and you can press Enter to continue or type feedback.\n\n"
            "Accepted gate names:\n"
            "  analysis       pause after architect      (shows analysis.md, requirements.md)\n"
            "  tester         pause after tester         (shows test-plan.md)\n"
            "  review         pause after reviewer       (shows test-review.md)\n"
            "  tests          pause after test-generator\n"
            "  coder          pause after coder          (shows implementation.md)\n"
            "  qa             pause after qa-tester      (shows qa-report.md)\n"
            "  code-review    pause after code-reviewer  (shows code-review.md)\n"
            "  commit         pause after commit         (shows commit.md)\n"
            "  docs           pause after documentation  (shows docs.md, pr-description.md)\n\n"
            "Full agent names (architect, qa-tester, etc.) are also accepted.\n"
            "Example: --gates analysis,coder,qa,code-review"
        ),
    )
    parser.add_argument("prompt", help="Task description")
    args = parser.parse_args()

    repo_path = str(Path(args.repo).resolve())
    if not Path(repo_path).exists():
        print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    logger.info("mode=%s repo=%s", args.mode, repo_path)
    logger.info("task: %s", args.prompt)

    if args.mode == "auto":
        from agents.orchestrator import OrchestratorAgent
        agent = OrchestratorAgent(repo_path)
    else:
        gates = [g.strip() for g in args.gates.split(",") if g.strip()]
        from agents.delegated_orchestrator import DelegatedOrchestratorAgent
        agent = DelegatedOrchestratorAgent(repo_path, gates=gates)

    result = agent.run(args.prompt)

    print("\n" + "=" * 60)
    print(result["output"])
    print("=" * 60)

    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
