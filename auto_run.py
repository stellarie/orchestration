"""
Auto-orchestration entry point.

Usage:
    python auto_run.py --repo <path-to-repo> "your task description"
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from logging_config import setup_logging
setup_logging()

import logging
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Run the full agent pipeline with DeepSeek as orchestrator."
    )
    parser.add_argument("prompt", help="Task description")
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the target repository (default: current directory)",
    )
    args = parser.parse_args()

    repo_path = str(Path(args.repo).resolve())
    if not Path(repo_path).exists():
        print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    from agents.orchestrator import OrchestratorAgent

    logger.info("Starting auto-orchestration | repo=%s", repo_path)
    logger.info("Task: %s", args.prompt)

    agent  = OrchestratorAgent(repo_path)
    result = agent.run(args.prompt)

    print("\n" + "=" * 60)
    print(result["output"])
    print("=" * 60)

    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
