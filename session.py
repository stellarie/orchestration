import json
from pathlib import Path


class SessionManager:
    def __init__(self, repo_path: str):
        self.dir = Path(repo_path) / ".blackboard" / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self, agent: str) -> list:
        path = self.dir / f"{agent}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, agent: str, messages: list):
        path = self.dir / f"{agent}.json"
        path.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear_all(self):
        for f in self.dir.glob("*.json"):
            f.unlink()
