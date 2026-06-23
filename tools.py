import os
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

_WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web. Tries Tavily → DuckDuckGo → Bing automatically. Returns titles, URLs, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (default 8, max 20)"},
            },
            "required": ["query"],
        },
    },
}

_CRAWL_LINKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "crawl_links",
        "description": (
            "Fetch a URL and return all hyperlinks found on that page (href + anchor text, resolved to absolute URLs). "
            "Use this to discover related pages, API docs, or sub-sections from an index page, then fetch the ones that look relevant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url":       {"type": "string",  "description": "URL to crawl"},
                "max_links": {"type": "integer", "description": "Max links to return (default 40)"},
                "timeout":   {"type": "integer", "description": "Request timeout in seconds (default 20)"},
            },
            "required": ["url"],
        },
    },
}

_FETCH_URL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch the text content of a URL (HTML stripped to readable text, truncated to 20k chars).",
        "parameters": {
            "type": "object",
            "properties": {
                "url":     {"type": "string", "description": "Full URL to fetch"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 20)"},
            },
            "required": ["url"],
        },
    },
}

_GITHUB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "github_search",
        "description": "Search GitHub for repositories or issues via the GitHub REST API.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "GitHub search query (supports qualifiers like language:python, label:good-first-issue)"},
                "type":  {"type": "string", "description": "'repositories' or 'issues' (default: repositories)"},
                "sort":  {"type": "string", "description": "Sort field: stars, forks, updated, created (default: stars)"},
                "max_results": {"type": "integer", "description": "Max results (default 10, max 30)"},
            },
            "required": ["query"],
        },
    },
}

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
_WRITE_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_output",
        "description": (
            "Write a deliverable file to the project output directory (NOT the blackboard). "
            "Use for final, human-readable outputs: research briefs, analysis reports, action plans, scout findings. "
            "The file is written to {repo_path}/{filename} and a metadata header is auto-injected. "
            "Call this IN ADDITION TO write_blackboard — both are required for deliverable files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Relative output path, e.g. 'research/brief.md' or 'oss/overview.md'",
                },
                "content": {
                    "type": "string",
                    "description": "File content (metadata header is added automatically — do not include it)",
                },
            },
            "required": ["filename", "content"],
        },
    },
}

_WRITE_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_memory",
        "description": (
            "Record a learning or insight to your global memory. "
            "Persists across ALL projects and pipeline runs. "
            "Write things that make you more effective in future runs: "
            "successful patterns, pitfalls, domain rules, version-specific facts, judge critique patterns. "
            "Be specific and concrete — skip vague observations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "What you learned. Concrete and specific.",
                },
            },
            "required": ["content"],
        },
    },
}

_ALL_TOOL_SCHEMAS: dict[str, dict] = {
    t["function"]["name"]: t for t in TOOL_SCHEMAS
}
_ALL_TOOL_SCHEMAS["read_contract_file"] = _READ_CONTRACT_FILE_SCHEMA
_ALL_TOOL_SCHEMAS["web_search"]         = _WEB_SEARCH_SCHEMA
_ALL_TOOL_SCHEMAS["crawl_links"]        = _CRAWL_LINKS_SCHEMA
_ALL_TOOL_SCHEMAS["fetch_url"]          = _FETCH_URL_SCHEMA
_ALL_TOOL_SCHEMAS["github_search"]      = _GITHUB_SEARCH_SCHEMA
_ALL_TOOL_SCHEMAS["write_output"]       = _WRITE_OUTPUT_SCHEMA
_ALL_TOOL_SCHEMAS["write_memory"]       = _WRITE_MEMORY_SCHEMA


def build_tool_schemas(tool_names: set[str]) -> list[dict]:
    """Return the API schema list for the given set of allowed tool names.

    Tool names not in _ALL_TOOL_SCHEMAS are silently skipped (e.g. orchestrator
    tools added outside this module).
    """
    return [_ALL_TOOL_SCHEMAS[name] for name in sorted(tool_names) if name in _ALL_TOOL_SCHEMAS]


class ToolExecutor:
    def __init__(self, repo_path: str, blackboard, write_deny: list[str] | None = None,
                 agent_name: str = "", pipeline_id: str = "", memory=None, output_dir: str = ""):
        self.repo          = Path(repo_path)
        self.bb            = blackboard
        self._write_deny   = write_deny or []
        self._agent_name   = agent_name
        self._pipeline_id  = pipeline_id
        self._memory       = memory
        self._output_dir   = output_dir

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
            elif name == "write_output":
                return self._write_output(args["filename"], args["content"])
            elif name == "write_memory":
                return self._write_memory(args["content"])
            elif name == "run_command":
                return self._run_command(args["command"], args.get("timeout", 60))
            elif name == "web_search":
                return self._web_search(args["query"], args.get("max_results", 8))
            elif name == "crawl_links":
                return self._crawl_links(args["url"], args.get("max_links", 40), args.get("timeout", 20))
            elif name == "fetch_url":
                return self._fetch_url(args["url"], args.get("timeout", 20))
            elif name == "github_search":
                return self._github_search(args["query"], args.get("type", "repositories"),
                                           args.get("sort", "stars"), args.get("max_results", 10))
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool error ({name}): {e}"

    def _write_output(self, filename: str, content: str) -> str:
        from datetime import datetime, timezone
        base = Path(self._output_dir) if self._output_dir else self.repo
        path = base / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        header = (
            f"---\nagent: {self._agent_name}\n"
            f"pipeline: {self._pipeline_id}\n"
            f"generated: {ts}\n---\n\n"
        )
        path.write_text(header + content, encoding="utf-8")
        return f"Written output: {filename}"

    def _write_memory(self, content: str) -> str:
        if self._memory is None:
            return "[write_memory] Memory manager not available."
        self._memory.append(self._agent_name, content)
        return "Memory updated."

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

    def _web_search(self, query: str, max_results: int = 8) -> str:
        errors: list[str] = []

        # 1. Tavily
        api_key = os.getenv("TAVILY_API_KEY")
        if api_key:
            try:
                r = self._web_search_tavily(query, max_results, api_key)
                if r:
                    return r
            except Exception as e:
                errors.append(f"tavily: {e}")

        # 2. DuckDuckGo
        try:
            from ddgs import DDGS
            results = list(DDGS().text(query, max_results=min(max_results, 20)))
            if results:
                lines = [f"Query: {query} (via DuckDuckGo)\n"]
                for i, r in enumerate(results, 1):
                    lines.append(
                        f"{i}. {r.get('title', '')}\n"
                        f"   URL: {r.get('href', '')}\n"
                        f"   {r.get('body', '')[:400]}\n"
                    )
                return "\n".join(lines)
        except ImportError:
            pass
        except Exception as e:
            errors.append(f"duckduckgo: {e}")

        # 3. Bing (scraping fallback)
        try:
            r = self._web_search_bing(query, max_results)
            if r:
                return r
        except Exception as e:
            errors.append(f"bing: {e}")

        err = "; ".join(errors) if errors else "no backend available"
        return (
            f"[web_search] All backends failed ({err}). "
            "Install ddgs for free search: pip install ddgs"
        )

    def _web_search_tavily(self, query: str, max_results: int, api_key: str) -> str:
        import urllib.request, json as _json
        max_results = min(max_results, 20)
        payload = _json.dumps({"api_key": api_key, "query": query, "max_results": max_results}).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            return ""
        lines = [f"Query: {query} (via Tavily)\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}\n   URL: {r.get('url', '')}\n   {r.get('content', '')[:400]}\n")
        return "\n".join(lines)

    def _web_search_bing(self, query: str, max_results: int = 8) -> str:
        import urllib.request, urllib.parse, json as _json
        params = urllib.parse.urlencode({"q": query, "count": min(max_results * 2, 20)})
        url = f"https://www.bing.com/search?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
                "Gecko/20100101 Firefox/124.0"
            ),
            "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT":             "1",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Each organic result lives in <li class="b_algo">
        algo_blocks = re.findall(
            r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL,
        )
        results = []
        for block in algo_blocks[:max_results]:
            m = re.search(
                r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                block, re.DOTALL,
            )
            if not m:
                continue
            link  = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            sp    = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
            snip  = re.sub(r"<[^>]+>", " ", sp.group(1)).strip()[:400] if sp else ""
            results.append((title, link, snip))

        if not results:
            return ""
        lines = [f"Query: {query} (via Bing)\n"]
        for i, (title, link, snip) in enumerate(results, 1):
            lines.append(f"{i}. {title}\n   URL: {link}\n   {snip}\n")
        return "\n".join(lines)

    def _fetch_url(self, url: str, timeout: int = 20) -> str:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research-agent)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ct  = resp.headers.get("Content-Type", "")
            text = raw.decode("utf-8", errors="replace")
            if "html" in ct.lower():
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>",  "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text[:20000]
        except Exception as e:
            return f"[fetch_url] Error fetching {url}: {e}"

    def _crawl_links(self, url: str, max_links: int = 40, timeout: int = 20) -> str:
        try:
            import urllib.request
            from urllib.parse import urljoin, urlparse, urldefrag
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research-agent)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw       = resp.read()
                ct        = resp.headers.get("Content-Type", "")
                final_url = resp.url
            if "html" not in ct.lower():
                return f"[crawl_links] Not an HTML page (Content-Type: {ct})"
            html = raw.decode("utf-8", errors="replace")

            # Page title
            tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
            page_title = re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else final_url

            # Extract all <a href="...">anchor</a>
            link_pat = re.compile(
                r'<a[^>]+href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            seen: set[str] = set()
            links: list[tuple[str, str]] = []
            for m in link_pat.finditer(html):
                raw_href = m.group(1).strip()
                anchor   = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if not anchor:
                    continue
                abs_href, _ = urldefrag(urljoin(final_url, raw_href))
                p = urlparse(abs_href)
                if p.scheme not in ("http", "https") or not p.netloc:
                    continue
                if abs_href in seen:
                    continue
                seen.add(abs_href)
                links.append((abs_href, anchor[:100]))
                if len(links) >= max_links:
                    break

            lines = [
                f"Page: {page_title}",
                f"URL:  {final_url}",
                f"Links found: {len(links)}\n",
            ]
            for i, (href, anchor) in enumerate(links, 1):
                lines.append(f"{i:3}. {anchor}\n     {href}")
            return "\n".join(lines)
        except Exception as e:
            return f"[crawl_links] Error: {e}"

    def _github_search(self, query: str, type: str = "repositories",
                       sort: str = "stars", max_results: int = 10) -> str:
        try:
            import urllib.request, urllib.parse, json as _json
            max_results = min(max_results, 30)
            params = urllib.parse.urlencode({"q": query, "sort": sort, "per_page": max_results})
            endpoint = "issues" if type == "issues" else "repositories"
            url = f"https://api.github.com/search/{endpoint}?{params}"
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "research-agent"}
            token = os.getenv("GITHUB_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read())
            items = data.get("items", [])
            lines = [f"GitHub search: {query} (type={type}, sort={sort})\n"]
            for i, item in enumerate(items, 1):
                if endpoint == "repositories":
                    lines.append(
                        f"{i}. {item.get('full_name', '')} ★{item.get('stargazers_count', 0)}\n"
                        f"   {item.get('description', '')}\n"
                        f"   {item.get('html_url', '')}\n"
                        f"   Lang: {item.get('language', 'N/A')}  "
                        f"Open issues: {item.get('open_issues_count', 0)}\n"
                    )
                else:
                    lines.append(
                        f"{i}. [{item.get('state', '')}] {item.get('title', '')}\n"
                        f"   {item.get('html_url', '')}\n"
                        f"   Labels: {', '.join(l['name'] for l in item.get('labels', []))}\n"
                    )
            return "\n".join(lines) if items else "No results found."
        except Exception as e:
            return f"[github_search] Error: {e}"
