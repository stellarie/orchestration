import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class BlackBoard:
    def __init__(self, repo_path: str):
        self.root         = Path(repo_path) / ".blackboard"
        self.sessions_dir = self.root / "sessions"
        self.memory_dir   = self.root / "agent-memory"
        self._status_lock = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.root.mkdir(exist_ok=True)
        self.sessions_dir.mkdir(exist_ok=True)
        self.memory_dir.mkdir(exist_ok=True)

    def read(self, filename: str) -> str:
        path = self.root / filename
        if not path.exists():
            return f"[{filename} does not exist yet]"
        return path.read_text(encoding="utf-8")

    def write(self, filename: str, content: str, append: bool = False):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if append and path.exists():
            content = path.read_text(encoding="utf-8") + "\n" + content
        path.write_text(content, encoding="utf-8")

    def init_task(self, task_id: str, description: str):
        now = datetime.now(timezone.utc).isoformat()
        self.write("task.md", f"# Task\n\n{description}\n\n_Task ID: {task_id} | Started: {now}_")
        self.write("status.json", json.dumps({
            "task_id":      task_id,
            "current_step": "architect",
            "started_at":   now,
            "steps": {
                name: {"status": "pending", "iterations": 0}
                for name in [
                    "architect", "designer", "planner", "scaffolder",
                    "tester", "reviewer", "test-generator",
                    "coder", "qa-tester", "code-reviewer",
                    "commit", "documentation",
                ]
            },
        }, indent=2))
        for f in self.sessions_dir.glob("*.json"):
            f.unlink()

    def get_status(self) -> dict:
        raw = self.read("status.json")
        if raw.startswith("["):
            return {}
        return json.loads(raw)

    def update_status(self, step: str, status: str, increment_iteration: bool = False):
        with self._status_lock:
            s = self.get_status()
            if not s:
                return
            s["current_step"] = step
            s["steps"][step]["status"] = status
            if increment_iteration:
                s["steps"][step]["iterations"] += 1
            self.write("status.json", json.dumps(s, indent=2))
