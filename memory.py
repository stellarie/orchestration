import os
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

COMPACT_THRESHOLD = 200_000  # chars before auto-compaction (~50k tokens)
COMPACT_TARGET    =  40_000  # chars to compact down to (~10k tokens)

_MEMORY_DIR = Path(__file__).parent / "agent-memory"


class MemoryManager:
    def __init__(self):
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = _MEMORY_DIR

    def read(self, agent: str) -> str | None:
        path = self.dir / f"{agent}.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def append(self, agent: str, content: str):
        path = self.dir / f"{agent}.md"
        ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n\n## {ts}\n{content.strip()}"
        base  = path.read_text(encoding="utf-8") if path.exists() else f"# {agent} global learnings\n"
        path.write_text(base + entry, encoding="utf-8")
        if path.stat().st_size > COMPACT_THRESHOLD:
            self._compact(agent, path)

    def _compact(self, agent: str, path: Path):
        try:
            import urllib.request, json as _json
            api_key = os.getenv("DEEPSEEK_API_KEY")
            if not api_key:
                return
            content = path.read_text(encoding="utf-8")
            payload = _json.dumps({
                "model": "deepseek-v4-pro",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"You are compacting the global memory of the '{agent}' agent. "
                            "Summarize the entries into a dense, structured set of rules and learnings. "
                            "Preserve specific patterns, pitfalls, domain rules, and version-specific facts. "
                            "Remove redundant, outdated, or vague entries. "
                            f"Target output: under {COMPACT_TARGET} characters. "
                            f"Start with a '# {agent} global learnings' header."
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                "max_tokens": 8000,
            }).encode()
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = _json.loads(resp.read())
            compacted = data["choices"][0]["message"]["content"].strip()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            path.write_text(
                f"# {agent} global learnings\n_Auto-compacted {ts}_\n\n{compacted}\n",
                encoding="utf-8",
            )
            logger.info("[memory] compacted %s memory (now %d chars)", agent, path.stat().st_size)
        except Exception:
            logger.exception("[memory] compaction failed for %s", agent)
