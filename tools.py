import re
import subprocess
from pathlib import Path

# Patterns that identify implementation files the test-generator must not read.
# Matches: *Impl.java, */impl/*, */implementation/*, *.impl.ts, etc.
_IMPL_PATTERNS: list[re.Pattern] = [
    re.compile(r'.*[Ii]mpl\.[a-zA-Z]+$'),        # SomethingImpl.java / .ts / .js
    re.compile(r'.*[/\\]impl[/\\].*'),             # any */impl/* directory
    re.compile(r'.*[/\\]implementation[/\\].*'),   # any */implementation/* directory
]


def _is_implementation_file(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(pattern.match(p) for pattern in _IMPL_PATTERNS)


def _path_matches_deny(path: str, pattern: str) -> bool:
    """Match a repo-relative path against a single deny-glob pattern.

    Supports two wildcard forms:
      prefix/**   — matches the prefix directory itself or anything under it
      **/suffix   — matches the suffix pattern in any subdirectory (e.g. **/*.test.ts)
    Falls back to standard fnmatch for everything else.
    """
    from fnmatch import fnmatch
    p = path.replace("\\", "/")
    pat = pattern.replace("\\", "/")

    if pat.endswith("/**"):
        prefix = pat[:-3]
        return p == prefix or p.startswith(prefix + "/")

    if pat.startswith("**/"):
        suffix_pat = pat[3:]
        filename = p.rsplit("/", 1)[-1]
        return fnmatch(filename, suffix_pat)

    return fnmatch(p, pat)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the repository. Creates parent dirs if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path relative to repo root"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the repository. Returns relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory relative to repo root. Use '.' for root."},
                    "pattern":   {"type": "string", "description": "Optional glob pattern (e.g. '**/*.py')"},
                },
                "required": ["directory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_blackboard",
            "description": "Read a file from the shared blackboard (.blackboard/ directory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename in .blackboard/ (e.g. 'requirements.md')"}
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_blackboard",
            "description": "Write to a file in the shared blackboard (.blackboard/ directory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename in .blackboard/ (e.g. 'analysis.md')"},
                    "content":  {"type": "string", "description": "Content to write"},
                    "append":   {"type": "boolean", "description": "Append to existing content instead of overwriting. Default false."},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the repository root. Returns stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Default 60."},
                },
                "required": ["command"],
            },
        },
    },
]

READ_ONLY_TOOLS = [t for t in TOOL_SCHEMAS if t["function"]["name"] != "run_command"]

_READ_CONTRACT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_contract_file",
        "description": (
            "Read an interface, DTO, type definition, model, entity, repository interface, "
            "or test/config file from the repository. "
            "Intentionally CANNOT read implementation files (*Impl.java, */impl/*, etc.) — "
            "you are writing tests against contracts and should not see business logic. "
            "Use this instead of read_file for all source file reads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"}
            },
            "required": ["path"],
        },
    },
}

# Tools for the test-generator: can write test files and run commands,
# but read_file is replaced by read_contract_file (blocks impl files).
CONTRACT_TOOLS = (
    [t for t in TOOL_SCHEMAS if t["function"]["name"] != "read_file"]
    + [_READ_CONTRACT_FILE_SCHEMA]
)

# Lookup map from tool name → schema dict; used by build_tool_schemas().
_ALL_TOOL_SCHEMAS: dict[str, dict] = {
    t["function"]["name"]: t for t in TOOL_SCHEMAS
}
_ALL_TOOL_SCHEMAS["read_contract_file"] = _READ_CONTRACT_FILE_SCHEMA


def build_tool_schemas(tool_names: set[str]) -> list[dict]:
    """Return the API schema list for the given set of allowed tool names.

    Tool names not in _ALL_TOOL_SCHEMAS are silently skipped (e.g. orchestrator
    tools added outside this module).
    """
    return [_ALL_TOOL_SCHEMAS[name] for name in sorted(tool_names) if name in _ALL_TOOL_SCHEMAS]


class ToolExecutor:
    def __init__(self, repo_path: str, blackboard, write_deny: list[str] | None = None):
        self.repo        = Path(repo_path)
        self.bb          = blackboard
        self._write_deny = write_deny or []

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "read_file":
                return self._read_file(args["path"])
            elif name == "read_contract_file":
                return self._read_contract_file(args["path"])
            elif name == "write_file":
                return self._write_file(args["path"], args["content"])
            elif name == "list_files":
                return self._list_files(args["directory"], args.get("pattern"))
            elif name == "read_blackboard":
                return self.bb.read(args["filename"])
            elif name == "write_blackboard":
                self.bb.write(args["filename"], args["content"], append=args.get("append", False))
                return f"Written to blackboard: {args['filename']}"
            elif name == "run_command":
                return self._run_command(args["command"], args.get("timeout", 60))
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool error ({name}): {e}"

    def _read_file(self, path: str) -> str:
        full = self.repo / path
        if not full.exists():
            return f"File not found: {path}"
        try:
            return full.read_text(encoding="utf-8")
        except Exception as e:
            return f"Could not read {path}: {e}"

    def _read_contract_file(self, path: str) -> str:
        if _is_implementation_file(path):
            return (
                f"[read_contract_file] BLOCKED: '{path}' looks like an implementation file "
                f"(*Impl.*, */impl/*, */implementation/*). "
                f"You must not read implementations while writing tests — "
                f"derive the contract from the interface, DTO, or design-spec.md instead."
            )
        return self._read_file(path)

    def _write_file(self, path: str, content: str) -> str:
        norm = path.replace("\\", "/")
        for pattern in self._write_deny:
            if _path_matches_deny(norm, pattern):
                return (
                    f"[WRITE BLOCKED] '{path}' matches deny pattern '{pattern}'. "
                    f"This agent is not permitted to write to that path."
                )
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"Written: {path}"

    def _list_files(self, directory: str, pattern: str = None) -> str:
        base = self.repo / directory
        if not base.exists():
            return f"Directory not found: {directory}"
        if pattern:
            files = list(base.glob(pattern))
        else:
            files = [f for f in base.rglob("*") if f.is_file()]
        files = [f for f in files if ".blackboard" not in f.parts]
        relative = sorted(str(f.relative_to(self.repo)) for f in files[:200])
        return "\n".join(relative) if relative else "No files found"

    def _run_command(self, command: str, timeout: int = 60) -> str:
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if result.stdout:
            parts.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            parts.append(f"STDERR:\n{result.stderr}")
        parts.append(f"Exit code: {result.returncode}")
        return "\n".join(parts)
