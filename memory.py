from datetime import datetime, timezone
from pathlib import Path


class MemoryManager:
    def __init__(self, repo_path: str):
        self.dir = Path(repo_path) / ".blackboard" / "agent-memory"
        self.dir.mkdir(parents=True, exist_ok=True)

    def read(self, agent: str) -> str | None:
        path = self.dir / f"{agent}.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def append(self, agent: str, content: str):
        path = self.dir / f"{agent}.md"
        ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n\n## {ts}\n{content.strip()}"
        base  = path.read_text(encoding="utf-8") if path.exists() else f"# {agent} episodic memory\n"
        path.write_text(base + entry, encoding="utf-8")
