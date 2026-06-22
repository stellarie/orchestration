import json
import logging
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from dataclasses import dataclass
from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    ANTHROPIC_API_KEY,
    MODELS, MAX_TOOL_ITERATIONS, AGENT_TOOL_ITERATIONS, AGENT_CAPABILITIES,
)
from blackboard import BlackBoard
from session import SessionManager
from memory import MemoryManager
from tools import TOOL_SCHEMAS, READ_ONLY_TOOLS, CONTRACT_TOOLS, ToolExecutor, build_tool_schemas
from grimoire import load_for as _load_grimoire
import queue as _queue
from event_bus import bus, interrupt_bus


# ── Lightweight wrappers that mirror the OpenAI response shape ───────────────
# Used by _call_anthropic_api so the rest of the agentic loop needs no branching.

@dataclass
class _AnthFunction:
    name: str
    arguments: str   # JSON string — same as OpenAI's tc.function.arguments

@dataclass
class _AnthToolCall:
    id: str
    function: "_AnthFunction"
    type: str = "function"

@dataclass
class _AnthMessage:
    content: str | None
    tool_calls: list | None
    reasoning_content: str | None

class _AnthChoice:
    def __init__(self, message: _AnthMessage) -> None:
        self.message = message

class _AnthResponse:
    def __init__(self, message: _AnthMessage) -> None:
        self.choices = [_AnthChoice(message)]

PROMPTS_DIR   = Path(__file__).parent / "prompts"
_ORCH_LOG_DIR = Path(__file__).parent.parent / "logs"
logger = logging.getLogger(__name__)


import re as _re


def _coerce_json(s: str) -> str:
    s = s.strip()
    s = _re.sub(r'\bTrue\b',  'true',  s)
    s = _re.sub(r'\bFalse\b', 'false', s)
    s = _re.sub(r'\bNone\b',  'null',  s)
    s = _re.sub(r',\s*([}\]])', r'\1', s)  # trailing commas
    return s


def _parse_json(s: str | None) -> tuple[dict, bool]:
    """Return (parsed, was_coerced). Raises JSONDecodeError if unparseable."""
    raw = s or "{}"
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        return json.loads(_coerce_json(raw)), True


def _is_valid_json(s: str | None) -> bool:
    try:
        _parse_json(s)
        return True
    except (json.JSONDecodeError, Exception):
        return False


class BaseAgent:
    NAME          = ""
    READ_ONLY     = False
    CONTRACT_ONLY = False  # test-generator: read_file replaced by read_contract_file

    def __init__(self, repo_path: str, force_deepseek: bool = False, pipeline_id: str = ""):
        self.repo_path   = repo_path
        self.pipeline_id = pipeline_id
        self.bb          = BlackBoard(repo_path)
        self.session    = SessionManager(repo_path)
        self.memory     = MemoryManager(repo_path)
        caps            = AGENT_CAPABILITIES.get(self.NAME, {})
        self.executor   = ToolExecutor(repo_path, self.bb, write_deny=caps.get("write_deny", []))
        self.cfg        = dict(MODELS[self.NAME])
        if force_deepseek and self.cfg.get("provider") == "anthropic":
            self.cfg["provider"] = "deepseek"
            self.cfg["model"]    = "deepseek-v4-pro"
        self.client       = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        self._log_path:   Path | None          = None  # resolved lazily on first write
        self._interrupt_q: _queue.Queue | None = None  # registered at run()-time

    def _emit(self, type: str, data: str = "") -> None:
        bus.emit({
            "type":        type,
            "agent":       self.NAME,
            "repo_path":   self.repo_path,
            "pipeline_id": self.pipeline_id,
            "data":        data,
            "ts":          datetime.now().isoformat(),
        })

    def run(
        self,
        instruction:    str,
        resume_session: bool = False,
        output_suffix:  str  = "",
    ) -> dict:
        self._interrupt_q = interrupt_bus.register(self.NAME)
        self._emit("agent_start")
        try:
            # Drain messages queued before this run started and prepend them
            # to the instruction so the agent addresses them first.
            pending = []
            while True:
                try:
                    pending.append(self._interrupt_q.get_nowait())
                except _queue.Empty:
                    break
            for msg in pending:
                self._emit("user_interrupt", msg)

            if pending:
                block = "\n\n".join(f"<user_message>\n{m}\n</user_message>" for m in pending)
                instruction = (
                    f"Before proceeding with your pipeline task, address these messages "
                    f"from the user:\n\n{block}\n\n---\n\n{instruction}"
                )

            episodic = self.memory.read(self.NAME)
            system   = self._build_system(episodic, output_suffix, instruction)
            messages = self.session.load(self.NAME) if resume_session else []
            messages.append({"role": "user", "content": instruction})

            caps = AGENT_CAPABILITIES.get(self.NAME)
            if caps is not None:
                tools = build_tool_schemas(caps["tools"])
            else:
                tools = (CONTRACT_TOOLS if self.CONTRACT_ONLY
                         else READ_ONLY_TOOLS if self.READ_ONLY
                         else TOOL_SCHEMAS)
            result = self._agentic_loop(messages, system, tools)
            self.session.save(self.NAME, self._strip_reasoning(messages))
            self._emit("agent_done")
            return result
        except Exception:
            self._emit("agent_failed")
            raise
        finally:
            interrupt_bus.unregister(self.NAME)
            self._interrupt_q = None

    def _load_prompt(self) -> str:
        path = PROMPTS_DIR / f"{self.NAME}.md"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return ""

    def _build_system(self, episodic: str | None, output_suffix: str = "", instruction: str = "") -> str:
        parts = [self._load_prompt()]
        if output_suffix:
            parts.append(
                f"\n## Parallel instance — output file suffix: `{output_suffix}`\n"
                f"You are one of several instances running in parallel.\n\n"
                f"**Blackboard files**: append `{output_suffix}` to every blackboard filename you write. "
                f"Examples: `implementation{output_suffix}.md`, `thinking/{self.NAME}{output_suffix}.md`. "
                f"Never write to the unsuffixed canonical filename.\n\n"
                f"**Scope lock — source files**: only create or modify source files explicitly named "
                f"in your instruction. Never touch shared infrastructure files "
                f"(package.json, tsconfig.json, requirements.txt, go.mod, pyproject.toml, "
                f"pom.xml, build.gradle, Dockerfile, docker-compose.yml, .env, .env.example, "
                f"jest.config.*, vite.config.*, webpack.config.*) unless they are the explicitly "
                f"assigned file in your instruction. "
                f"If your implementation needs a new dependency, document it in your blackboard file "
                f"— a sequential step before or after this batch handles shared infrastructure."
            )
        if episodic:
            parts.append(f"\n## Your memory of this repo\n{episodic}")
        grimoire = _load_grimoire(self.NAME, instruction)
        if grimoire:
            parts.append(f"\n{grimoire}")
        return "\n".join(parts)

    # ── Thinking / tool-call log ─────────────────────────────────────────────

    def _get_log_path(self) -> Path:
        """Resolve the per-run log file path, cached after first call.

        Reads `run-id` from the blackboard (written by /task/init).
        Falls back to a timestamp if the pipeline was started without init.
        """
        if self._log_path is None:
            _ORCH_LOG_DIR.mkdir(exist_ok=True)
            run_id = self.bb.read("run-id").strip()
            if not run_id or run_id.startswith("["):
                run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = _ORCH_LOG_DIR / f"{run_id}.log"
        return self._log_path

    def _log_thinking(self, text: str) -> None:
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"--- {ts} [{self.NAME}]\n{text.strip()}\n---\n"
        try:
            with self._get_log_path().open("a", encoding="utf-8") as f:
                f.write(entry)
        except OSError as e:
            logger.warning("[%s] thinking log write failed: %s", self.NAME, e)

    def _log_tool_call(self, name: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._get_log_path().open("a", encoding="utf-8") as f:
                f.write(f"{ts} [{self.NAME}] → {name}\n")
        except OSError as e:
            logger.warning("[%s] tool-call log write failed: %s", self.NAME, e)

    # ── Agentic loop ─────────────────────────────────────────────────────────

    def _agentic_loop(self, messages: list, system: str, tools: list) -> dict:
        all_thinking: list[str] = []
        max_steps = AGENT_TOOL_ITERATIONS.get(self.NAME, MAX_TOOL_ITERATIONS)

        for step in range(max_steps):
            # Drain any user-injected messages before the next API call
            while self._interrupt_q:
                try:
                    user_msg = self._interrupt_q.get_nowait()
                except _queue.Empty:
                    break
                logger.info("[%s] user interrupt: %s", self.NAME, user_msg[:120])
                self._emit("user_interrupt", user_msg)
                messages.append({"role": "user", "content": user_msg})

            response   = self._call_api(messages, system, tools)
            msg        = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            thinking   = getattr(msg, "reasoning_content", None)

            if thinking:
                all_thinking.append(f"### Step {step + 1}\n{thinking}")
                logger.debug("[%s] step %d thinking:\n%s", self.NAME, step + 1, thinking)
                self._log_thinking(thinking)
                self._emit("thinking", thinking)

            assistant_entry = {
                "role":    "assistant",
                "content": msg.content or "",
            }
            if tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id":       tc.id,
                        "type":     "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_entry)

            if not tool_calls:
                self._write_thinking(all_thinking)
                if msg.content:
                    self._emit("message", msg.content)
                return {
                    "status":    "success",
                    "output":    msg.content or "",
                    "reasoning": thinking,
                }

            for tc in tool_calls:
                self._log_tool_call(tc.function.name)
                self._emit("tool_call", tc.function.name)
                try:
                    args, coerced = _parse_json(tc.function.arguments)
                    if coerced:
                        logger.warning(
                            "[%s] coerced malformed tool arguments for %s | raw: %r",
                            self.NAME, tc.function.name, tc.function.arguments,
                        )
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning(
                        "[%s] unparseable tool arguments for %s: %s | raw: %r",
                        self.NAME, tc.function.name, exc, tc.function.arguments,
                    )
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "content":      (
                            f"Error: could not parse arguments as JSON ({exc}). "
                            f"Please retry with valid JSON arguments."
                        ),
                    })
                    continue
                tool_result = self.executor.execute(tc.function.name, args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      str(tool_result),
                })

        self._write_thinking(all_thinking)
        return {
            "status":    "max_iterations",
            "output":    "Agent reached max tool iterations without finishing.",
            "reasoning": None,
        }

    def _write_thinking(self, chunks: list[str]) -> None:
        if not chunks:
            return
        content = "\n\n".join(chunks)
        self.bb.write(f"thinking/{self.NAME}.md", content)
        logger.info("[%s] thinking logged (%d chars, %d steps)", self.NAME, len(content), len(chunks))

    # ── Anthropic helpers ────────────────────────────────────────────────────

    @staticmethod
    def _openai_tools_to_anthropic(tools: list) -> list:
        """Convert OpenAI-format tool schemas to Anthropic format."""
        result = []
        for t in tools:
            fn = t["function"]
            result.append({
                "name":         fn["name"],
                "description":  fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    @staticmethod
    def _openai_messages_to_anthropic(messages: list) -> list:
        """Convert an OpenAI-format message history to Anthropic format.

        Key differences:
          - assistant tool_calls  → content array with tool_use blocks
          - role "tool" messages  → user message with tool_result blocks
            (consecutive tool results are merged into one user message)
        """
        result: list[dict] = []
        for msg in messages:
            role = msg["role"]

            if role == "user":
                result.append({"role": "user", "content": msg.get("content", "")})

            elif role == "assistant":
                blocks: list[dict] = []
                text = msg.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in (msg.get("tool_calls") or []):
                    blocks.append({
                        "type":  "tool_use",
                        "id":    tc["id"],
                        "name":  tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    })
                result.append({"role": "assistant", "content": blocks})

            elif role == "tool":
                block = {
                    "type":        "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content":     msg.get("content", ""),
                }
                # Merge with the previous user message if it already holds tool_results
                if (result
                        and result[-1]["role"] == "user"
                        and isinstance(result[-1]["content"], list)
                        and result[-1]["content"]
                        and result[-1]["content"][0].get("type") == "tool_result"):
                    result[-1]["content"].append(block)
                else:
                    result.append({"role": "user", "content": [block]})

        return result

    def _call_anthropic_api(self, messages: list, system: str, tools: list) -> _AnthResponse:
        """Call the Anthropic SDK and return a response wrapped in the OpenAI-like shape."""
        from anthropic import Anthropic

        client   = Anthropic(api_key=ANTHROPIC_API_KEY)
        budget   = self.cfg.get("budget_tokens", 10000)
        kwargs: dict = {
            "model":      self.cfg["model"],
            "max_tokens": budget + 8192,
            "system":     system,
            "tools":      self._openai_tools_to_anthropic(tools),
            "messages":   self._openai_messages_to_anthropic(messages),
        }
        if self.cfg.get("thinking"):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

        response = client.messages.create(**kwargs)

        text_parts: list[str]    = []
        thinking_parts: list[str] = []
        tool_calls: list[_AnthToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(_AnthToolCall(
                    id=block.id,
                    function=_AnthFunction(
                        name=block.name,
                        arguments=json.dumps(block.input),
                    ),
                ))

        return _AnthResponse(_AnthMessage(
            content          = "\n".join(text_parts) or None,
            tool_calls       = tool_calls or None,
            reasoning_content= "\n\n".join(thinking_parts) or None,
        ))

    # ── Unified API dispatch ─────────────────────────────────────────────────

    def _call_api(self, messages: list, system: str, tools: list, max_retries: int = 3):
        if self.cfg.get("provider") == "anthropic":
            if ANTHROPIC_API_KEY:
                # Key present: use Claude (from .env, or inherited from the running Claude Code session)
                return self._call_anthropic_api(messages, system, tools)
            else:
                # No key — fall back to DeepSeek with max thinking so reasoning quality is preserved
                logger.warning(
                    "[%s] ANTHROPIC_API_KEY not set — falling back to DeepSeek v4 Pro (thinking=max). "
                    "Set ANTHROPIC_API_KEY in .env or run via Claude Code to use %s.",
                    self.NAME, self.cfg["model"],
                )
                fallback = {"model": "deepseek-v4-pro", "thinking": True, "reasoning_effort": "max"}
                return self._call_deepseek(messages, system, tools, cfg=fallback, max_retries=max_retries)

        return self._call_deepseek(messages, system, tools, cfg=self.cfg, max_retries=max_retries)

    def _call_deepseek(self, messages: list, system: str, tools: list,
                       cfg: dict | None = None, max_retries: int = 3):
        cfg = cfg or self.cfg
        kwargs = {
            "model":       cfg["model"],
            "messages":    [{"role": "system", "content": system}] + messages,
            "tools":       tools,
            "tool_choice": "auto",
            "max_tokens":  108000,
        }
        if cfg.get("thinking"):
            kwargs["extra_body"]       = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = cfg.get("reasoning_effort", "max")

        for attempt in range(max_retries):
            response   = self.client.chat.completions.create(**kwargs)
            tool_calls = getattr(response.choices[0].message, "tool_calls", None) or []
            bad        = [
                tc.function.name for tc in tool_calls
                if not _is_valid_json(tc.function.arguments)
            ]
            if not bad:
                return response
            logger.warning(
                "[%s] malformed tool arguments for %s (attempt %d/%d), retrying",
                self.NAME, bad, attempt + 1, max_retries,
            )

        return response  # last attempt — error handler in the loop takes over

    def _strip_reasoning(self, messages: list) -> list:
        clean = []
        for m in messages:
            if m.get("role") == "assistant" and "reasoning_content" in m:
                m = {k: v for k, v in m.items() if k != "reasoning_content"}
            clean.append(m)
        return clean
