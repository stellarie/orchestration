import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from agents.base import BaseAgent
from config import (
    MAX_TOOL_ITERATIONS, AGENT_TOOL_ITERATIONS,
    AGENT_CONCURRENCY, AGENT_MAX_CALLS, AGENT_OUTPUTS, MODELS,
)
from tools import READ_ONLY_TOOLS
from validators import validate_agent_output

logger = logging.getLogger(__name__)

# Imported lazily inside _run_pipeline_agent to avoid circular import issues
_PIPELINE_AGENTS: dict | None = None


def _get_pipeline_agents() -> dict:
    global _PIPELINE_AGENTS
    if _PIPELINE_AGENTS is None:
        from agents.architect import ArchitectAgent
        from agents.designer import DesignerAgent
        from agents.planner import PlannerAgent
        from agents.scaffolder import ScaffolderAgent
        from agents.tester import TesterAgent
        from agents.reviewer import ReviewerAgent
        from agents.test_generator import TestGeneratorAgent
        from agents.coder import CoderAgent
        from agents.qa_tester import QATesterAgent
        from agents.code_reviewer import CodeReviewerAgent
        from agents.commit import CommitAgent
        from agents.documentation import DocumentationAgent
        _PIPELINE_AGENTS = {
            "architect":      ArchitectAgent,
            "designer":       DesignerAgent,
            "planner":        PlannerAgent,
            "scaffolder":     ScaffolderAgent,
            "tester":         TesterAgent,
            "reviewer":       ReviewerAgent,
            "test-generator": TestGeneratorAgent,
            "coder":          CoderAgent,
            "qa-tester":      QATesterAgent,
            "code-reviewer":  CodeReviewerAgent,
            "commit":         CommitAgent,
            "documentation":  DocumentationAgent,
        }
    return _PIPELINE_AGENTS


_ALL_AGENTS = [
    "architect", "designer", "planner", "scaffolder", "tester", "reviewer",
    "test-generator", "coder", "qa-tester", "code-reviewer",
    "commit", "documentation",
]

RUN_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_agent",
        "description": (
            "Invoke a pipeline agent with an instruction. "
            "Blocks until the agent finishes and returns its output and status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": _ALL_AGENTS,
                    "description": "Which pipeline agent to run",
                },
                "instruction": {
                    "type": "string",
                    "description": "The instruction to give the agent",
                },
                "resume_session": {
                    "type": "boolean",
                    "description": "Resume the agent's previous session (use for coder retries)",
                },
            },
            "required": ["agent", "instruction"],
        },
    },
}

RUN_AGENTS_PARALLEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_agents_parallel",
        "description": (
            "Run multiple agent instances simultaneously, each with a different instruction. "
            "Use this to execute independent work units from the same batch in work-plan.md. "
            "All tasks run concurrently; results are returned when all complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of agent tasks to run in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {
                                "type": "string",
                                "enum": _ALL_AGENTS,
                                "description": "Which pipeline agent to run",
                            },
                            "instruction": {
                                "type": "string",
                                "description": "The specific instruction for this agent instance",
                            },
                        },
                        "required": ["agent", "instruction"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}

ORCHESTRATOR_TOOLS = READ_ONLY_TOOLS + [RUN_AGENT_SCHEMA, RUN_AGENTS_PARALLEL_SCHEMA]


class OrchestratorAgent(BaseAgent):
    NAME      = "orchestrator"
    READ_ONLY = True

    def run(self, task: str, resume_session: bool = False) -> dict:
        task_id = str(uuid.uuid4())[:8]
        self.bb.init_task(task_id, task)
        logger.info("[orchestrator] task %s initialized", task_id)

        episodic = self.memory.read(self.NAME)
        system   = self._build_system(episodic)
        messages = [{"role": "user", "content": task}]

        result = self._orchestrator_loop(messages, system)
        self.session.save(self.NAME, self._strip_reasoning(messages))
        return result

    # ── override points for subclasses ──────────────────────────────────────

    def _get_tools(self) -> list:
        return ORCHESTRATOR_TOOLS

    def _dispatch_tool(self, name: str, args: dict) -> str:
        if name == "run_agent":
            return self._run_pipeline_agent(args)
        if name == "run_agents_parallel":
            return self._run_agents_parallel_batch(args)
        return self.executor.execute(name, args)

    # ── core loop ────────────────────────────────────────────────────────────

    def _orchestrator_loop(self, messages: list, system: str) -> dict:
        all_thinking: list[str] = []
        tools     = self._get_tools()
        max_steps = AGENT_TOOL_ITERATIONS.get(self.NAME, MAX_TOOL_ITERATIONS)

        for step in range(max_steps):
            response   = self._call_api(messages, system, tools)
            msg        = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            thinking   = getattr(msg, "reasoning_content", None)

            if thinking:
                all_thinking.append(f"### Step {step + 1}\n{thinking}")
                logger.debug("[orchestrator] step %d thinking:\n%s", step + 1, thinking)

            assistant_entry = {"role": "assistant", "content": msg.content or ""}
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
                return {"status": "success", "output": msg.content or ""}

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[orchestrator] malformed tool arguments for %s: %s | raw: %r",
                        tc.function.name, exc, tc.function.arguments,
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
                tool_result = self._dispatch_tool(tc.function.name, args)
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      str(tool_result),
                })

        self._write_thinking(all_thinking)
        return {"status": "max_iterations", "output": "Orchestrator reached max tool iterations."}

    # ── single-agent runner ──────────────────────────────────────────────────

    def _run_pipeline_agent(self, args: dict) -> str:
        agent_name     = args["agent"]
        instruction    = args["instruction"]
        resume_session = args.get("resume_session", False)

        agents = _get_pipeline_agents()
        if agent_name not in agents:
            return json.dumps({"error": f"Unknown agent: {agent_name}"})

        # Loop detection — escalate if this agent has already hit its call cap
        max_calls = AGENT_MAX_CALLS.get(agent_name, 3)
        status    = self.bb.get_status()
        current   = status.get("steps", {}).get(agent_name, {}).get("iterations", 0)
        if current >= max_calls:
            logger.warning(
                "[orchestrator] %s hit loop cap (%d/%d), escalating",
                agent_name, current, max_calls,
            )
            return json.dumps({
                "agent":           agent_name,
                "status":          "escalated",
                "iterations":      current,
                "output": (
                    f"`{agent_name}` has been invoked {current} time(s) without resolving. "
                    f"Accept the best available output and proceed, or raise to the user."
                ),
                "rework_requests": [],
            })

        concurrency = AGENT_CONCURRENCY.get(agent_name, 1)
        self.bb.update_status(agent_name, "in_progress", increment_iteration=True)

        def _run_once(instr: str, resume: bool) -> str:
            if concurrency > 1:
                return self._run_parallel_agents(agent_name, instr, concurrency, agents)
            logger.info("[orchestrator] → %s", agent_name)
            r = agents[agent_name](self.repo_path).run(instr, resume_session=resume)
            return r["output"]

        output = _run_once(instruction, resume_session)

        # Output completeness check — retry once if incomplete
        val_passed, val_issues = validate_agent_output(agent_name, self.bb)
        if not val_passed:
            logger.warning(
                "[orchestrator] %s output incomplete — retrying once: %s",
                agent_name, val_issues,
            )
            retry_instr = (
                f"Your previous output is incomplete. Issues: {'; '.join(val_issues)}. "
                f"Re-read your blackboard output files, complete any missing sections, "
                f"and rewrite any truncated files in full."
            )
            self.bb.update_status(agent_name, "in_progress", increment_iteration=True)
            output = _run_once(retry_instr, resume_session=True)
            val_passed, val_issues = validate_agent_output(agent_name, self.bb)

        rework_requests = self._collect_rework_requests(agent_name)
        self.bb.update_status(agent_name, "done")
        logger.info("[orchestrator] ← %s: done  validation=%s", agent_name, "OK" if val_passed else "FAIL")
        if rework_requests:
            logger.info("[orchestrator] rework requests: %s", rework_requests)

        return json.dumps({
            "agent":           agent_name,
            "status":          "done",
            "output":          output,
            "validation":      {"passed": val_passed, "issues": val_issues},
            "rework_requests": rework_requests,
        })

    # ── parallel batch runner (different instructions per task) ─────────────

    def _run_agents_parallel_batch(self, args: dict) -> str:
        tasks = args.get("tasks", [])
        if not tasks:
            return json.dumps({"error": "tasks list is empty"})

        agents     = _get_pipeline_agents()
        wall_start = time.monotonic()
        logger.info(
            "[orchestrator|batch] submitting %d tasks — thread=%s",
            len(tasks), threading.current_thread().name,
        )

        def run_one(idx: int, task: dict) -> dict:
            agent_name  = task["agent"]
            instruction = task["instruction"]
            suffix      = f"-p{idx + 1}"
            tname       = threading.current_thread().name

            if agent_name not in agents:
                return {
                    "index":  idx,
                    "agent":  agent_name,
                    "status": "error",
                    "output": f"Unknown agent: {agent_name}",
                }

            # Per-task loop detection
            max_calls = AGENT_MAX_CALLS.get(agent_name, 3)
            status    = self.bb.get_status()
            current   = status.get("steps", {}).get(agent_name, {}).get("iterations", 0)
            if current >= max_calls:
                logger.warning(
                    "[orchestrator|batch] task %d: %s hit loop cap (%d/%d), escalating  thread=%s",
                    idx + 1, agent_name, current, max_calls, tname,
                )
                return {
                    "index":  idx,
                    "agent":  agent_name,
                    "status": "escalated",
                    "output": f"`{agent_name}` hit its loop cap ({current}/{max_calls}).",
                }

            t0 = time.monotonic()
            logger.info(
                "[orchestrator|batch] task %d/%d START  agent=%s suffix=%s  thread=%s",
                idx + 1, len(tasks), agent_name, suffix, tname,
            )
            self.bb.update_status(agent_name, "in_progress", increment_iteration=True)
            agent  = agents[agent_name](self.repo_path)
            result = agent.run(instruction, resume_session=False, output_suffix=suffix)

            final_status = "done" if result["status"] == "success" else "failed"
            self.bb.update_status(agent_name, final_status)
            elapsed = time.monotonic() - t0
            logger.info(
                "[orchestrator|batch] task %d/%d END    agent=%s status=%s elapsed=%.1fs  thread=%s",
                idx + 1, len(tasks), agent_name, final_status, elapsed, tname,
            )
            return {
                "index":  idx,
                "agent":  agent_name,
                "status": final_status,
                "output": result["output"],
            }

        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(run_one, i, task) for i, task in enumerate(tasks)]
            results = [f.result() for f in futures]

        wall_elapsed = time.monotonic() - wall_start
        succeeded    = sum(1 for r in results if r.get("status") == "done")
        logger.info(
            "[orchestrator|batch] %d/%d tasks succeeded  wall=%.1fs",
            succeeded, len(tasks), wall_elapsed,
        )

        # Collect all rework requests written during this batch
        rework_requests = self._collect_rework_requests("__batch__")

        return json.dumps({
            "batch_size":      len(tasks),
            "results":         results,
            "rework_requests": rework_requests,
        })

    # ── same-instruction parallel runner (AGENT_CONCURRENCY) ────────────────

    def _run_parallel_agents(
        self, agent_name: str, instruction: str, n: int, agents: dict
    ) -> str:
        wall_start = time.monotonic()
        logger.info(
            "[orchestrator|parallel] %s ×%d — submitting all instances now (thread=%s)",
            agent_name, n, threading.current_thread().name,
        )
        agent_cls = agents[agent_name]

        def run_instance(i: int) -> dict:
            suffix  = f"-{i + 1}"
            t0      = time.monotonic()
            tname   = threading.current_thread().name
            logger.info(
                "[orchestrator|parallel] %s instance %d/%d START  suffix=%s thread=%s",
                agent_name, i + 1, n, suffix, tname,
            )
            result = agent_cls(self.repo_path).run(
                instruction,
                resume_session=False,
                output_suffix=suffix,
            )
            elapsed = time.monotonic() - t0
            logger.info(
                "[orchestrator|parallel] %s instance %d/%d END    status=%s elapsed=%.1fs thread=%s",
                agent_name, i + 1, n, result.get("status"), elapsed, tname,
            )
            return result

        results: list[dict | None] = [None] * n
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {pool.submit(run_instance, i): i for i in range(n)}
            for future in futures:
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as exc:
                    logger.error(
                        "[orchestrator|parallel] %s instance %d raised: %s",
                        agent_name, i + 1, exc,
                    )
                    results[i] = {"status": "error", "output": str(exc)}

        wall_elapsed = time.monotonic() - wall_start
        successful   = sum(1 for r in results if r and r["status"] == "success")
        logger.info(
            "[orchestrator|parallel] %s ×%d DONE  %d/%d succeeded  wall=%.1fs",
            agent_name, n, successful, n, wall_elapsed,
        )
        return self._reconcile_outputs(agent_name, n, results)

    def _reconcile_outputs(
        self, agent_name: str, n: int, results: list[dict | None]
    ) -> str:
        output_files = AGENT_OUTPUTS.get(agent_name, [])
        if not output_files:
            return f"{agent_name} ×{n} complete (no blackboard files to reconcile)"

        summaries: list[str] = []

        for filename in output_files:
            stem, ext = filename.rsplit(".", 1)
            drafts: list[tuple[int, str]] = []

            for i in range(n):
                draft_name = f"{stem}-{i + 1}.{ext}"
                content    = self.bb.read(draft_name)
                if not content.startswith("["):
                    drafts.append((i + 1, content))

            if not drafts:
                logger.warning("[reconciler] no drafts found for %s", filename)
                summaries.append(f"no drafts for {filename}")
                continue

            if len(drafts) == 1:
                self.bb.write(filename, drafts[0][1])
                summaries.append(f"single draft copied → {filename}")
                logger.info("[reconciler] single draft → %s", filename)
                continue

            draft_sections = "\n\n".join(
                f"--- Draft {i} ---\n{content}" for i, content in drafts
            )
            prompt = (
                f"You are reconciling {len(drafts)} parallel drafts of `{filename}`.\n"
                f"Produce one best version: include the strongest elements from each, "
                f"resolve contradictions by choosing the most thorough option, remove duplicates.\n"
                f"Write the reconciled output directly — no preamble, no explanation.\n\n"
                f"{draft_sections}"
            )

            response       = self.client.chat.completions.create(
                model      = MODELS["orchestrator"]["model"],
                messages   = [{"role": "user", "content": prompt}],
                max_tokens = 108000,
            )
            choice         = response.choices[0]
            reconciled     = choice.message.content or ""
            finish_reason  = getattr(choice, "finish_reason", None)

            if finish_reason == "length":
                logger.warning(
                    "[reconciler] %s reconciliation hit token limit (finish_reason=length) — output may be truncated",
                    filename,
                )

            self.bb.write(filename, reconciled)
            note = " ⚠ truncated" if finish_reason == "length" else ""
            summaries.append(f"{len(drafts)} drafts reconciled → {filename}{note}")
            logger.info(
                "[reconciler] %d drafts → %s (%d chars, finish=%s)",
                len(drafts), filename, len(reconciled), finish_reason,
            )

        return "; ".join(summaries)

    def _collect_rework_requests(self, requesting_agent: str) -> list[dict]:
        rework_dir = Path(self.repo_path) / ".blackboard" / "rework"
        if not rework_dir.exists():
            return []
        requests = []
        for f in sorted(rework_dir.glob("*.md")):
            target = f.stem
            if target == requesting_agent:
                continue  # skip self-rework
            requests.append({
                "agent":  target,
                "reason": f.read_text(encoding="utf-8").strip(),
            })
            f.unlink()
        return requests
