import asyncio
import json
import logging
import time
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from logging_config import setup_logging
from blackboard import BlackBoard
from config import AGENT_CONCURRENCY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, AGENT_OUTPUTS
from event_bus import bus, interrupt_bus
from validators import validate_agent_output

setup_logging()
logger = logging.getLogger(__name__)

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
from agents.reconciler import ReconcilerAgent
from agents.consultant import ConsultantAgent
from agents.judge import JudgeAgent
from agents.query_planner import QueryPlannerAgent
from agents.oss_scout import OssScoutAgent
from agents.issue_auditor import IssueAuditorAgent
from agents.contribution_planner import ContributionPlannerAgent
from agents.searcher import SearcherAgent
from agents.reader import ReaderAgent
from agents.tech_auditor import TechAuditorAgent
from agents.research_synthesizer import ResearchSynthesizerAgent

AGENT_MAP = {
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
    "reconciler":     ReconcilerAgent,
    "consultant":     ConsultantAgent,
    "judge":               JudgeAgent,
    "query-planner":       QueryPlannerAgent,
    "searcher":            SearcherAgent,
    "reader":              ReaderAgent,
    "tech-auditor":        TechAuditorAgent,
    "research-synthesizer":  ResearchSynthesizerAgent,
    "oss-scout":             OssScoutAgent,
    "issue-auditor":         IssueAuditorAgent,
    "contribution-planner":  ContributionPlannerAgent,
}

app = FastAPI(title="Orchestration Server", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    bus.set_loop(asyncio.get_event_loop())


# ── request/response models ──────────────────────────────────────────────────

class RunRequest(BaseModel):
    agent:          str
    repo_path:      str
    instruction:    str
    resume_session: bool = False


class BatchTask(BaseModel):
    agent:       str
    instruction: str
    suffix:      str = ""  # optional override; auto-generated if empty


class RunBatchRequest(BaseModel):
    repo_path:     str
    tasks:         list[BatchTask]
    force_deepseek: bool = False  # True = auto mode (all DeepSeek)


class TaskInitRequest(BaseModel):
    repo_path:   str
    description: str


class BlackboardWriteRequest(BaseModel):
    repo_path: str
    filename:  str
    content:   str
    append:    bool = False


class PipelineRunRequest(BaseModel):
    repo_path:   str
    description: str
    skip_agents: list[str] = []


class PipelineResumeRequest(BaseModel):
    repo_path:   str
    skip_agents: list[str] = []


class ConsultantAskRequest(BaseModel):
    message:          str
    project_repo_path: str = ""  # optional; injected into instruction for blackboard context


class PipelineQueueRequest(BaseModel):
    repo_path:          str
    description:        str
    skip_agents:        list[str] = []
    agent_overrides:    dict[str, str] = {}  # agent_name → extra instruction prepended at run-time
    source_pipeline_id: str = ""             # pass handoff.md from this pipeline into task context


class PipelineUpdateRequest(BaseModel):
    pipeline_id:     str
    skip_agents:     list[str] = []
    agent_overrides: dict[str, str] = {}


class PipelineControlRequest(BaseModel):
    action: str  # "pause" | "resume" | "stop"


class AgentControlRequest(BaseModel):
    action: str  # "skip" | "restart" | "pause"


class ResearchRunRequest(BaseModel):
    repo_path:        str
    description:      str
    queue_dev:        bool = False   # auto-queue dev pipeline after research finishes
    dev_description:  str  = ""      # task for the queued dev pipeline (defaults to description)


class ScoutRunRequest(BaseModel):
    repo_path:        str            # workspace directory (not the cloned OSS repo)
    description:      str            # topic / interests for the scout
    queue_dev:        bool = False   # auto-queue dev pipeline once contribution-planner finishes
    dev_description:  str  = ""      # override task desc for queued dev pipeline


# ── Pipeline registry ────────────────────────────────────────────────────────

@dataclass
class PipelineEntry:
    pipeline_id:        str
    repo_path:          str
    task_desc:          str
    status:             str  # 'running' | 'queued' | 'done' | 'failed' | 'paused' | 'stopped'
    skip_agents:        set  = dc_field(default_factory=set)
    agent_overrides:    dict = dc_field(default_factory=dict)
    source_pipeline_id: str  = ""   # inject handoff.md from this pipeline when starting
    created_at:         str  = dc_field(default_factory=lambda: datetime.now().isoformat())


_pipeline_registry: dict[str, PipelineEntry] = {}
_pipeline_queues:   dict[str, list]           = {}   # repo_path → ordered list of queued pipeline_ids
_pipeline_control:     dict[str, str]                  = {}   # pipeline_id → "running"|"paused"|"stopped"
_agent_control:        dict[str, str]                  = {}   # "{pipeline_id}:{agent}" → "skip"|"restart"
_pipeline_stop_events: dict[str, threading.Event]      = {}   # pipeline_id → Event; set to kill mid-call
_reg_lock = threading.Lock()


def _emit_pipeline_event(event_type: str, pipeline_id: str, repo_path: str, data: str = ""):
    bus.emit({
        "type":        event_type,
        "pipeline_id": pipeline_id,
        "repo_path":   repo_path,
        "agent":       "",
        "data":        data,
        "ts":          datetime.now().isoformat(),
    })


def _write_handoff(entry: "PipelineEntry"):
    """Write a handoff.md summarising the pipeline's blackboard outputs for a queued successor."""
    bb = BlackBoard(entry.repo_path)
    lines = [
        f"# Pipeline handoff — {entry.pipeline_id}",
        f"Task: {entry.task_desc[:200]}",
        "",
        "## Outputs produced",
    ]
    for agent, files in AGENT_OUTPUTS.items():
        for fname in files:
            content = bb.read(fname)
            if content and not content.startswith("["):
                lines.append(f"\n### {fname} (from {agent})\n")
                lines.append(content[:3000])
                if len(content) > 3000:
                    lines.append(f"\n… (truncated, {len(content)} chars total)")
    bb.write("handoff.md", "\n".join(lines))
    logger.info("[pipeline] handoff.md written for %s", entry.pipeline_id)


def _run_pipeline_with_cleanup(entry: "PipelineEntry", start_step: int = 0):
    """Wrapper that runs a pipeline and handles queue advancement on finish."""
    _ensure_repo(entry.repo_path, entry.pipeline_id, entry.repo_path)
    stop_ev = threading.Event()
    _pipeline_stop_events[entry.pipeline_id] = stop_ev
    with _reg_lock:
        _pipeline_control[entry.pipeline_id] = "running"
    try:
        _run_pipeline_steps(
            entry.repo_path, entry.task_desc, start_step,
            entry.skip_agents, entry.pipeline_id, entry.agent_overrides,
        )
        ctrl = _pipeline_control.get(entry.pipeline_id, "running")
        final = "stopped" if ctrl == "stopped" else "done"
    except Exception:
        logger.exception("[pipeline] %s crashed", entry.pipeline_id)
        final = "failed"
    finally:
        _pipeline_stop_events.pop(entry.pipeline_id, None)

    with _reg_lock:
        if entry.pipeline_id in _pipeline_registry:
            _pipeline_registry[entry.pipeline_id].status = final

    if final == "done":
        try:
            _write_handoff(entry)
        except Exception:
            logger.warning("[pipeline] handoff write failed for %s", entry.pipeline_id)

    _emit_pipeline_event("pipeline_status", entry.pipeline_id, entry.repo_path, final)
    _start_next_queued(entry.repo_path)


def _start_next_queued(repo_path: str):
    """Pop the first queued pipeline for repo_path and start it."""
    with _reg_lock:
        queue = _pipeline_queues.get(repo_path, [])
        next_entry = None
        for pid in list(queue):
            e = _pipeline_registry.get(pid)
            if e and e.status == "queued":
                e.status = "running"
                _pipeline_queues[repo_path] = [p for p in queue if p != pid]
                next_entry = e
                break
    if not next_entry:
        return

    bb = BlackBoard(next_entry.repo_path)

    # If a source pipeline is set, inject its handoff and apply scout repo path
    if next_entry.source_pipeline_id:
        src = _pipeline_registry.get(next_entry.source_pipeline_id)
        if src:
            src_bb = BlackBoard(src.repo_path)

            # Scout→dev: read cloned repo path written by contribution-planner
            cloned_path_raw = src_bb.read("scout/cloned_repo_path.md")
            if cloned_path_raw and not cloned_path_raw.startswith("["):
                cloned_path = cloned_path_raw.strip()
                if cloned_path:
                    next_entry.repo_path = cloned_path
                    logger.info("[queue] scout handoff — routing dev pipeline to %s", cloned_path)
                    # Prefer contribution-brief as the task description
                    brief = src_bb.read("scout/contribution-brief.md")
                    if brief and not brief.startswith("["):
                        next_entry.task_desc = (
                            f"{next_entry.task_desc}\n\n"
                            f"---\n## Contribution brief from OSS scout\n\n"
                            f"{brief[:8000]}"
                        )

            # General handoff.md injection (research pipeline or any other source)
            handoff = src_bb.read("handoff.md")
            if handoff and not handoff.startswith("["):
                next_entry.task_desc = (
                    f"{next_entry.task_desc}\n\n"
                    f"---\n## Context from previous pipeline ({src.pipeline_id})\n\n"
                    f"{handoff[:6000]}"
                )

    bb.init_task(next_entry.pipeline_id, next_entry.task_desc)
    bb.write("run-id",        next_entry.pipeline_id)
    bb.write("pipeline-step", "0")
    _emit_pipeline_event("pipeline_status", next_entry.pipeline_id, next_entry.repo_path, "running")

    threading.Thread(
        target=_run_pipeline_with_cleanup,
        args=(next_entry,),
        daemon=True,
        name=f"pipeline-{next_entry.pipeline_id}",
    ).start()


# Pipeline step schema:
#   str                                  → single agent, once
#   {"agent": str, "count": int,
#    "reconcile": bool}                  → N parallel instances of same agent;
#                                          reconciler runs after if reconcile=True
#   [str | dict, ...]                    → fan-out group: all entries run in parallel
#                                          (mixed agents OK, reconcilers per-entry)
PIPELINE_STEPS = [
    {"agent": "architect",      "count": 3, "reconcile": True},
    "designer",
    "planner",
    "scaffolder",
    "tester",
    [
        {"agent": "reviewer",       "count": 2, "reconcile": True},
        {"agent": "test-generator", "count": 1, "reconcile": False},
    ],
    "coder",
    ["qa-tester", "code-reviewer"],
    "commit",
    "documentation",
]


OSS_SCOUT_STEPS = [
    "oss-scout",
    "issue-auditor",
    "contribution-planner",
]

RESEARCH_PIPELINE_STEPS = [
    "query-planner",
    {"agent": "searcher",  "count": 3, "reconcile": False},
    {"agent": "reader",    "count": 3, "reconcile": False},
    "tech-auditor",
    "research-synthesizer",
]


def _step_agent_names(step) -> list[str]:
    """Flat list of agent names involved in a step (including reconciler where relevant)."""
    if isinstance(step, str):
        return [step]
    if isinstance(step, dict):
        names = [step["agent"]]
        if step.get("reconcile") and step.get("count", 1) > 1:
            names.append("reconciler")
        return names
    if isinstance(step, list):
        result = []
        for s in step:
            result.extend(_step_agent_names(s))
        return result
    return []


def _determine_angles(task_description: str) -> list[str]:
    """Quick non-agentic call to get 3 complementary architect angles for the task."""
    import json as _json
    from openai import OpenAI
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": (
                "For this software task, suggest exactly 3 complementary, non-overlapping "
                "architect analysis angles. Return ONLY a JSON array of 3 short strings "
                "(each under 12 words). No explanation, no markdown, just the array.\n\n"
                f"Task: {task_description}"
            )}],
            max_tokens=150,
        )
        text = resp.choices[0].message.content.strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json\n").split("```")[0]
        return _json.loads(text)[:3]
    except Exception:
        logger.warning("[pipeline] angle determination failed, using defaults")
        return [
            "technical architecture, data model, and tech stack",
            "domain logic, user-facing features, and workflows",
            "reliability, edge cases, performance, and constraints",
        ]


def _run_pipeline_steps(
    repo_path:       str,
    task_description: str,
    start_step:      int  = 0,
    skip_agents:     set  | None = None,
    pipeline_id:     str  = "",
    agent_overrides: dict | None = None,
    steps_override:  list | None = None,
):
    """Execute PIPELINE_STEPS (or steps_override) from start_step. Called in a background thread."""
    STEPS = steps_override if steps_override is not None else PIPELINE_STEPS
    skip_agents     = skip_agents or set()
    agent_overrides = agent_overrides or {}
    bb              = BlackBoard(repo_path)
    base_instr      = (
        "Complete your pipeline role. "
        "The task is described in .blackboard/task.md. "
        "Read all relevant blackboard files from previous pipeline steps before acting."
    )

    def _wait_if_active(agent_name: str):
        while agent_name in interrupt_bus.active():
            logger.info("[pipeline] waiting for %s to finish independent run", agent_name)
            time.sleep(2)

    def _check_pipeline_control() -> str:
        """Return current control state; block while paused. Returns 'stopped' to abort."""
        while True:
            ctrl = _pipeline_control.get(pipeline_id, "running")
            if ctrl == "stopped":
                return "stopped"
            if ctrl != "paused":
                return ctrl
            time.sleep(1)

    def _run_one(agent_name: str, instruction: str, force_ds: bool, suffix: str = "") -> str:
        ctrl = _check_pipeline_control()
        if ctrl == "stopped":
            return "stopped"

        agent_key = f"{pipeline_id}:{agent_name}"
        action = _agent_control.pop(agent_key, None)
        if action == "skip":
            logger.info("[pipeline] skipping %s by user request", agent_name)
            bb.update_status(agent_name, "skipped")
            _emit_pipeline_event("agent_skipped", pipeline_id, repo_path, agent_name)
            return "done"

        _wait_if_active(agent_name)
        override = agent_overrides.get(agent_name, "").strip()
        if override:
            instruction = f"{override}\n\n---\n\n{instruction}"
        bb.update_status(agent_name, "in_progress", increment_iteration=True)
        try:
            agent = AGENT_MAP[agent_name](repo_path, force_deepseek=force_ds, pipeline_id=pipeline_id)
            agent.stop_event = _pipeline_stop_events.get(pipeline_id)
            result = agent.run(instruction, output_suffix=suffix)
            s = result["status"]
            status = "done" if s == "success" else "stopped" if s == "stopped" else "failed"
        except Exception:
            logger.exception("[pipeline] agent=%s crashed", agent_name)
            status = "failed"
        bb.update_status(agent_name, status)
        return status

    def _reconcile(agent_name: str, count: int, angles: list | None = None) -> str:
        outputs = AGENT_OUTPUTS.get(agent_name, [])
        if not outputs:
            return "done"
        drafts = [
            f"{Path(o).stem}-p{i+1}{Path(o).suffix}"
            for o in outputs for i in range(count)
        ]
        instr = (
            f"Reconcile the parallel {agent_name} outputs into a single authoritative result.\n"
            f"Read drafts from the blackboard: {', '.join(drafts)}\n"
            f"Write canonical files to the blackboard: {', '.join(outputs)}\n"
            f"Be opinionated: resolve every conflict, fill every gap, eliminate all ambiguity."
        )
        if angles:
            instr += f"\n\nAngles covered by the drafts: {angles}"
        return _run_one("reconciler", instr, force_ds=False)  # always use Anthropic

    JUDGE_MAX_ATTEMPTS = 5
    FULL_AUTO_JUDGE    = True  # True = auto-pass after max attempts; False = pause for human

    def _run_judge(agent_name: str, attempt: int) -> str:
        """Run the judge after agent_name completed. Returns 'pass', 'rework:{critique}', or 'escalate'."""
        outputs   = AGENT_OUTPUTS.get(agent_name, [])
        retry_req = bb.read(f"retry-request/{agent_name}.md") or ""
        if retry_req.startswith("["):
            retry_req = ""
        instr = (
            f"Evaluate the output just produced by the '{agent_name}' agent (attempt {attempt+1}).\n\n"
            f"Output files to check: {', '.join(outputs) if outputs else '(none — check blackboard manually)'}\n"
            f"Also read: task.md, and retry-request/{agent_name}.md if it exists.\n\n"
            + (f"Previous rework feedback (now addressed or not):\n{retry_req}\n\n" if retry_req else "")
            + "Return PASS, REWORK {agent}: {critique}, or ESCALATE: {reason}."
        )
        try:
            agent  = JudgeAgent(repo_path, pipeline_id=pipeline_id)
            result = agent.run(instr)
            verdict = (result.get("output") or "").strip()
        except Exception:
            logger.exception("[judge] crashed evaluating %s", agent_name)
            verdict = "PASS"  # don't block pipeline on judge crash
        bb.write(f"judge/{agent_name}-attempt{attempt+1}.md", verdict)
        logger.info("[judge] %s attempt %d → %s", agent_name, attempt+1, verdict[:80])
        return verdict

    def _run_one_with_judge(agent_name: str, base_instruction: str, force_ds: bool, suffix: str = "") -> str:
        """Run an agent, then judge its output, retrying up to JUDGE_MAX_ATTEMPTS."""
        instruction = base_instruction
        for attempt in range(JUDGE_MAX_ATTEMPTS):
            status = _run_one(agent_name, instruction, force_ds, suffix)
            if status in ("failed", "stopped", "skipped"):
                return status

            verdict = _run_judge(agent_name, attempt)

            if verdict.upper().startswith("PASS"):
                return "done"
            elif verdict.upper().startswith("ESCALATE"):
                reason = verdict.split(":", 1)[1].strip() if ":" in verdict else verdict
                if FULL_AUTO_JUDGE:
                    bb.write(f"judge-caveats/{agent_name}.md",
                             f"Auto-passed after ESCALATE (attempt {attempt+1}/{JUDGE_MAX_ATTEMPTS}):\n{reason}")
                    logger.warning("[judge] ESCALATE on %s — auto-passing (full-auto mode)", agent_name)
                    return "done"
                else:
                    with _reg_lock:
                        if pipeline_id in _pipeline_registry:
                            _pipeline_registry[pipeline_id].status = "paused"
                    _pipeline_control[pipeline_id] = "paused"
                    _emit_pipeline_event("pipeline_status", pipeline_id, repo_path,
                                        f"paused_escalated:{agent_name}:{reason[:120]}")
                    _check_pipeline_control()  # blocks until resumed or stopped
                    return _pipeline_control.get(pipeline_id, "running") == "stopped" and "stopped" or "done"
            else:
                # REWORK
                critique = verdict.split(":", 1)[1].strip() if ":" in verdict else verdict
                instruction = (
                    f"Rework requested by judge (attempt {attempt+1}/{JUDGE_MAX_ATTEMPTS}):\n\n"
                    f"{critique}\n\n---\n\n{base_instruction}"
                )

        # Exhausted attempts — auto-pass with caveat
        bb.write(f"judge-caveats/{agent_name}.md",
                 f"Max judge attempts ({JUDGE_MAX_ATTEMPTS}) reached. Proceeding with caveats.")
        logger.warning("[judge] %s exhausted max attempts — auto-passing", agent_name)
        return "done"

    angles_cache: list | None = None

    for step_idx, step in enumerate(STEPS[start_step:], start=start_step):
        # Skip step if every non-reconciler agent in it is in the skip list
        step_names = [n for n in _step_agent_names(step) if n != "reconciler"]
        if step_names and all(n in skip_agents for n in step_names):
            logger.info("[pipeline] skipping step %d: %s", step_idx, step)
            bb.write("pipeline-step", str(step_idx + 1))
            continue

        logger.info("[pipeline] step %d: %s", step_idx, step)

        if isinstance(step, str):
            # ── single agent ────────────────────────────────────────────────
            status = _run_one_with_judge(step, base_instr, True)
            if status in ("failed", "stopped"):
                logger.warning("[pipeline] stopping at step %d (%s): %s", step_idx, step, status)
                return

        elif isinstance(step, dict):
            # ── N parallel instances of same agent ───────────────────────────
            agent_name = step["agent"]
            count      = step.get("count", 1)
            reconcile  = step.get("reconcile", False)

            if count == 1:
                status = _run_one_with_judge(agent_name, base_instr, True)
                if status in ("failed", "stopped"):
                    return
            else:
                if agent_name == "architect":
                    angles_cache = _determine_angles(task_description)
                    instrs = [f"{base_instr}\n\nYour analysis angle: {a}" for a in angles_cache]
                else:
                    instrs = [base_instr] * count

                with ThreadPoolExecutor(max_workers=count) as pool:
                    futures  = [pool.submit(_run_one, agent_name, instrs[i], True, f"-p{i+1}") for i in range(count)]
                    statuses = [f.result() for f in futures]

                if any(s in ("failed", "stopped") for s in statuses):
                    logger.warning("[pipeline] parallel %s had failures/stop", agent_name)
                    return

                if reconcile:
                    status = _reconcile(agent_name, count, angles_cache if agent_name == "architect" else None)
                    if status == "failed":
                        return
                    # Judge the reconciled output
                    verdict = _run_judge(agent_name, 0)
                    if verdict.upper().startswith("ESCALATE"):
                        logger.warning("[judge] escalate on reconciled %s output", agent_name)

        elif isinstance(step, list):
            # ── fan-out group (different agents / entries) ───────────────────
            tasks = [s if isinstance(s, dict) else {"agent": s, "count": 1, "reconcile": False} for s in step]
            max_w = sum(t.get("count", 1) for t in tasks)

            with ThreadPoolExecutor(max_workers=max_w) as pool:
                futures = []
                for t in tasks:
                    agent_name = t["agent"]
                    count      = t.get("count", 1)
                    for i in range(count):
                        suffix = f"-p{i+1}" if count > 1 else ""
                        futures.append((t, pool.submit(_run_one, agent_name, base_instr, True, suffix)))

                results = [(t, f.result()) for t, f in futures]

            if any(status in ("failed", "stopped") for _, status in results):
                logger.warning("[pipeline] fan-out group had failures/stop at step %d", step_idx)
                return

            # Reconcile entries that need it
            for t in tasks:
                if t.get("reconcile") and t.get("count", 1) > 1:
                    status = _reconcile(t["agent"], t["count"])
                    if status == "failed":
                        return

            # Judge each fan-out entry individually after the group completes
            for t in tasks:
                if t.get("count", 1) == 1:
                    verdict = _run_judge(t["agent"], 0)
                    if verdict.upper().startswith("REWORK") or verdict.upper().startswith("ESCALATE"):
                        logger.info("[judge] fan-out %s → %s", t["agent"], verdict[:60])

        bb.write("pipeline-step", str(step_idx + 1))


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "anthropic_available": bool(ANTHROPIC_API_KEY)}


@app.get("/pipeline/steps")
def get_pipeline_steps():
    """Return the PIPELINE_STEPS structure so the frontend can render step groups."""
    def _serialise(step):
        if isinstance(step, str):
            return {"type": "single", "agent": step}
        if isinstance(step, dict):
            return {"type": "parallel", "agent": step["agent"],
                    "count": step.get("count", 1), "reconcile": step.get("reconcile", False)}
        if isinstance(step, list):
            return {"type": "fanout", "entries": [_serialise(s) for s in step]}
        return {}
    return [_serialise(s) for s in PIPELINE_STEPS]


@app.post("/task/init")
def init_task(req: TaskInitRequest):
    _require_repo(req.repo_path)
    task_id = str(uuid.uuid4())[:8]
    bb = BlackBoard(req.repo_path)
    bb.init_task(task_id, req.description)
    bb.write("run-id", task_id)   # agents read this to name their per-run log file
    return {"task_id": task_id, "status": "initialized"}


@app.post("/run")
def run_agent(req: RunRequest):
    if req.agent not in AGENT_MAP:
        raise HTTPException(400, f"Unknown agent '{req.agent}'. Valid: {list(AGENT_MAP)}")
    _require_repo(req.repo_path)

    bb          = BlackBoard(req.repo_path)
    concurrency = AGENT_CONCURRENCY.get(req.agent, 1)
    bb.update_status(req.agent, "in_progress", increment_iteration=True)

    if concurrency > 1:
        # Spawn N parallel instances and reconcile — reuse orchestrator's logic
        from agents.orchestrator import OrchestratorAgent, _get_pipeline_agents
        orch   = OrchestratorAgent(req.repo_path)
        agents = _get_pipeline_agents()
        output = orch._run_parallel_agents(req.agent, req.instruction, concurrency, agents)
        result = {"status": "success", "output": output}
    else:
        agent  = AGENT_MAP[req.agent](req.repo_path)
        result = agent.run(req.instruction, resume_session=req.resume_session)

    # Output completeness check — retry once if incomplete
    val_passed, val_issues = validate_agent_output(req.agent, bb)
    if not val_passed:
        logger.warning("[server] %s output incomplete, retrying once: %s", req.agent, val_issues)
        retry_instr = (
            f"Your previous output is incomplete. Issues: {'; '.join(val_issues)}. "
            f"Re-read your blackboard output files, complete any missing sections, "
            f"and rewrite any truncated files in full."
        )
        bb.update_status(req.agent, "in_progress", increment_iteration=True)
        if concurrency > 1:
            output = orch._run_parallel_agents(req.agent, retry_instr, concurrency, agents)
            result = {"status": "success", "output": output}
        else:
            agent  = AGENT_MAP[req.agent](req.repo_path)
            result = agent.run(retry_instr, resume_session=True)
        val_passed, val_issues = validate_agent_output(req.agent, bb)

    final_status = "done" if result["status"] == "success" else "failed"
    bb.update_status(req.agent, final_status)

    return {**result, "validation": {"passed": val_passed, "issues": val_issues}}


@app.post("/pipeline/run")
def pipeline_run(req: PipelineRunRequest):
    """Start the full pipeline in a background thread (full-auto, all DeepSeek).

    Returns immediately; progress arrives via /stream SSE.
    """
    _ensure_repo(req.repo_path)
    pipeline_id = str(uuid.uuid4())[:8]
    entry = PipelineEntry(
        pipeline_id=pipeline_id,
        repo_path=req.repo_path,
        task_desc=req.description,
        status="running",
        skip_agents=set(req.skip_agents),
    )
    with _reg_lock:
        _pipeline_registry[pipeline_id] = entry

    bb = BlackBoard(req.repo_path)
    bb.init_task(pipeline_id, req.description)
    bb.write("run-id",        pipeline_id)
    bb.write("pipeline-step", "0")

    _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, "running")

    threading.Thread(
        target=_run_pipeline_with_cleanup,
        args=(entry,),
        daemon=True,
        name=f"pipeline-{pipeline_id}",
    ).start()
    return {"pipeline_id": pipeline_id, "task_id": pipeline_id, "status": "started"}


@app.post("/pipeline/resume")
def pipeline_resume(req: PipelineResumeRequest):
    """Resume a pipeline that stopped partway through.

    Reads the last completed step index and task description from the blackboard,
    then continues from the next step.
    """
    _require_repo(req.repo_path)
    bb = BlackBoard(req.repo_path)

    task_desc = bb.read("task.md") or ""
    if not task_desc:
        raise HTTPException(400, "No task found in blackboard — start a new run instead")

    raw_step  = bb.read("pipeline-step") or "0"
    try:
        start_step = int(raw_step.strip())
    except ValueError:
        start_step = 0

    if start_step >= len(PIPELINE_STEPS):
        return {"status": "already_complete", "step": start_step}

    pipeline_id = str(uuid.uuid4())[:8]
    logger.info("[pipeline/resume] resuming from step %d", start_step)
    entry = PipelineEntry(
        pipeline_id=pipeline_id,
        repo_path=req.repo_path,
        task_desc=task_desc,
        status="running",
        skip_agents=set(req.skip_agents),
    )
    with _reg_lock:
        _pipeline_registry[pipeline_id] = entry

    _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, "running")

    threading.Thread(
        target=_run_pipeline_with_cleanup,
        args=(entry, start_step),
        daemon=True,
        name=f"pipeline-resume-{pipeline_id}",
    ).start()
    return {"pipeline_id": pipeline_id, "task_id": pipeline_id, "status": "resumed", "from_step": start_step}


@app.post("/research/run")
def research_run(req: ResearchRunRequest):
    """Run the research pipeline, optionally queuing a dev pipeline after."""
    _ensure_repo(req.repo_path)
    pipeline_id = str(uuid.uuid4())[:8]
    entry = PipelineEntry(
        pipeline_id=pipeline_id,
        repo_path=req.repo_path,
        task_desc=req.description,
        status="running",
    )
    with _reg_lock:
        _pipeline_registry[pipeline_id] = entry

    bb = BlackBoard(req.repo_path)
    bb.init_task(pipeline_id, req.description)
    bb.write("run-id",        pipeline_id)
    bb.write("pipeline-step", "0")
    _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, "running")

    dev_pid = None
    if req.queue_dev:
        dev_pid = str(uuid.uuid4())[:8]
        dev_desc = req.dev_description or req.description
        dev_entry = PipelineEntry(
            pipeline_id=dev_pid,
            repo_path=req.repo_path,
            task_desc=dev_desc,
            status="queued",
            source_pipeline_id=pipeline_id,
        )
        with _reg_lock:
            _pipeline_registry[dev_pid] = dev_entry
            _pipeline_queues.setdefault(req.repo_path, []).append(dev_pid)
        _emit_pipeline_event("pipeline_queued", dev_pid, req.repo_path, dev_desc[:60])

    def _run_research():
        stop_ev = threading.Event()
        _pipeline_stop_events[pipeline_id] = stop_ev
        try:
            _run_pipeline_steps(req.repo_path, req.description, 0, set(), pipeline_id, {},
                                steps_override=RESEARCH_PIPELINE_STEPS)
            ctrl = _pipeline_control.get(pipeline_id, "running")
            final = "stopped" if ctrl == "stopped" else "done"
        except Exception:
            logger.exception("[research] %s crashed", pipeline_id)
            final = "failed"
        finally:
            _pipeline_stop_events.pop(pipeline_id, None)
        with _reg_lock:
            if pipeline_id in _pipeline_registry:
                _pipeline_registry[pipeline_id].status = final
        if final == "done":
            try:
                _write_handoff(entry)
            except Exception:
                pass
        _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, final)
        _start_next_queued(req.repo_path)

    threading.Thread(target=_run_research, daemon=True, name=f"research-{pipeline_id}").start()
    result = {"pipeline_id": pipeline_id, "status": "started"}
    if dev_pid:
        result["dev_pipeline_id"] = dev_pid
    return result


@app.post("/scout/run")
def scout_run(req: ScoutRunRequest):
    """Run the OSS scout pipeline, optionally queuing a dev pipeline after contribution-planner finishes."""
    _ensure_repo(req.repo_path)
    pipeline_id = str(uuid.uuid4())[:8]
    entry = PipelineEntry(
        pipeline_id=pipeline_id,
        repo_path=req.repo_path,
        task_desc=req.description,
        status="running",
    )
    with _reg_lock:
        _pipeline_registry[pipeline_id] = entry

    bb = BlackBoard(req.repo_path)
    bb.init_task(pipeline_id, req.description)
    bb.write("run-id",        pipeline_id)
    bb.write("pipeline-step", "0")
    _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, "running")

    dev_pid = None
    if req.queue_dev:
        dev_pid = str(uuid.uuid4())[:8]
        dev_desc = req.dev_description or req.description
        dev_entry = PipelineEntry(
            pipeline_id=dev_pid,
            repo_path=req.repo_path,   # will be overridden by _start_next_queued after cloning
            task_desc=dev_desc,
            status="queued",
            source_pipeline_id=pipeline_id,
        )
        with _reg_lock:
            _pipeline_registry[dev_pid] = dev_entry
            _pipeline_queues.setdefault(req.repo_path, []).append(dev_pid)
        _emit_pipeline_event("pipeline_queued", dev_pid, req.repo_path, dev_desc[:60])

    def _run_scout():
        stop_ev = threading.Event()
        _pipeline_stop_events[pipeline_id] = stop_ev
        try:
            _run_pipeline_steps(req.repo_path, req.description, 0, set(), pipeline_id, {},
                                steps_override=OSS_SCOUT_STEPS)
            ctrl = _pipeline_control.get(pipeline_id, "running")
            final = "stopped" if ctrl == "stopped" else "done"
        except Exception:
            logger.exception("[scout] %s crashed", pipeline_id)
            final = "failed"
        finally:
            _pipeline_stop_events.pop(pipeline_id, None)
        with _reg_lock:
            if pipeline_id in _pipeline_registry:
                _pipeline_registry[pipeline_id].status = final
        if final == "done":
            try:
                _write_handoff(entry)
            except Exception:
                pass
        _emit_pipeline_event("pipeline_status", pipeline_id, req.repo_path, final)
        _start_next_queued(req.repo_path)

    threading.Thread(target=_run_scout, daemon=True, name=f"scout-{pipeline_id}").start()
    result = {"pipeline_id": pipeline_id, "status": "started"}
    if dev_pid:
        result["dev_pipeline_id"] = dev_pid
    return result


@app.post("/consultant/ask")
def consultant_ask(req: ConsultantAskRequest):
    """Activate the consultant with optional project context.

    The consultant always runs from the orchestration directory so it can
    read the pipeline source code. The project blackboard path is injected
    into the instruction so it can reference it with absolute paths.
    """
    orchestration_path = str(Path(__file__).parent)

    instruction = req.message
    preamble_lines = [
        f"Orchestration directory (your working base): {orchestration_path}",
        f"IMPORTANT: All tool calls require ABSOLUTE paths.",
        f"  • To browse orchestration code: list_files(\"{orchestration_path}\")",
    ]
    if req.project_repo_path:
        preamble_lines += [
            f"  • To browse the user's project:  list_files(\"{req.project_repo_path}\")",
            f"  • Project blackboard:             list_files(\"{req.project_repo_path}/.blackboard\")",
        ]
    instruction = "\n".join(preamble_lines) + f"\n\n---\n\n{req.message}"

    if "consultant" in interrupt_bus.active():
        result = interrupt_bus.send("consultant", instruction)
        return {"agent": "consultant", "status": "queued", **result}

    bb = BlackBoard(orchestration_path)
    bb.update_status("consultant", "in_progress", increment_iteration=True)

    def _run():
        try:
            agent  = ConsultantAgent(orchestration_path)
            result = agent.run(instruction)
            status = "done" if result["status"] == "success" else "failed"
        except Exception:
            logger.exception("[consultant] crashed")
            status = "failed"
        bb.update_status("consultant", status)

    threading.Thread(target=_run, daemon=True, name="consultant").start()
    return {"agent": "consultant", "status": "started"}


@app.post("/pipeline/queue")
def pipeline_queue(req: PipelineQueueRequest):
    """Queue a pipeline to run after the current one on the same repo finishes."""
    _ensure_repo(req.repo_path)
    pipeline_id = str(uuid.uuid4())[:8]
    entry = PipelineEntry(
        pipeline_id=pipeline_id,
        repo_path=req.repo_path,
        task_desc=req.description,
        status="queued",
        skip_agents=set(req.skip_agents),
        agent_overrides=req.agent_overrides,
        source_pipeline_id=req.source_pipeline_id,
    )
    with _reg_lock:
        _pipeline_registry[pipeline_id] = entry
        _pipeline_queues.setdefault(req.repo_path, []).append(pipeline_id)
        position = len(_pipeline_queues[req.repo_path])

    _emit_pipeline_event("pipeline_queued", pipeline_id, req.repo_path, req.description[:60])
    return {"pipeline_id": pipeline_id, "status": "queued", "position": position}


@app.patch("/pipeline/update")
def pipeline_update(req: PipelineUpdateRequest):
    """Update skip_agents and agent_overrides on a queued pipeline."""
    with _reg_lock:
        entry = _pipeline_registry.get(req.pipeline_id)
        if not entry:
            raise HTTPException(404, f"Pipeline '{req.pipeline_id}' not found")
        if entry.status != "queued":
            raise HTTPException(400, "Can only update queued pipelines")
        entry.skip_agents    = set(req.skip_agents)
        entry.agent_overrides = req.agent_overrides
    return {"status": "updated"}


@app.post("/pipeline/{pipeline_id}/control")
def pipeline_control(pipeline_id: str, req: PipelineControlRequest):
    """Pause, resume, or stop a running pipeline."""
    with _reg_lock:
        entry = _pipeline_registry.get(pipeline_id)
        if not entry:
            raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
        if req.action == "pause":
            _pipeline_control[pipeline_id] = "paused"
            entry.status = "paused"
        elif req.action == "resume":
            _pipeline_control[pipeline_id] = "running"
            if entry.status == "paused":
                entry.status = "running"
        elif req.action == "stop":
            _pipeline_control[pipeline_id] = "stopped"
            entry.status = "stopped"
            stop_ev = _pipeline_stop_events.get(pipeline_id)
            if stop_ev:
                stop_ev.set()
            interrupt_bus.send_stop(pipeline_id) if hasattr(interrupt_bus, "send_stop") else None
        else:
            raise HTTPException(400, f"Unknown action '{req.action}'")
    _emit_pipeline_event("pipeline_status", pipeline_id, entry.repo_path, entry.status)
    return {"pipeline_id": pipeline_id, "status": entry.status}


@app.post("/pipeline/{pipeline_id}/agent/{agent_name}/control")
def agent_control(pipeline_id: str, agent_name: str, req: AgentControlRequest):
    """Skip, restart, or pause a specific agent in a running pipeline."""
    with _reg_lock:
        entry = _pipeline_registry.get(pipeline_id)
        if not entry:
            raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
        if req.action not in ("skip", "restart", "pause"):
            raise HTTPException(400, f"Unknown action '{req.action}'")
        if req.action == "pause":
            interrupt_bus.send(agent_name, "__pause__")
        else:
            _agent_control[f"{pipeline_id}:{agent_name}"] = req.action
    return {"pipeline_id": pipeline_id, "agent": agent_name, "action": req.action}


@app.get("/pipelines")
def list_pipelines():
    """Return all known pipelines (running, queued, done, failed)."""
    with _reg_lock:
        return [
            {
                "pipeline_id":        e.pipeline_id,
                "repo_path":          e.repo_path,
                "task_desc":          e.task_desc,
                "status":             e.status,
                "skip_agents":        list(e.skip_agents),
                "agent_overrides":    e.agent_overrides,
                "source_pipeline_id": e.source_pipeline_id,
                "created_at":         e.created_at,
            }
            for e in _pipeline_registry.values()
        ]


@app.post("/run-batch")
def run_batch(req: RunBatchRequest):
    """Run multiple agents in parallel, each with a different instruction.

    Each task gets an auto-generated output suffix (-p1, -p2, …) so their
    blackboard files do not collide. Returns when all tasks complete.
    """
    _require_repo(req.repo_path)
    for task in req.tasks:
        if task.agent not in AGENT_MAP:
            raise HTTPException(400, f"Unknown agent '{task.agent}'. Valid: {list(AGENT_MAP)}")

    bb         = BlackBoard(req.repo_path)
    wall_start = time.monotonic()
    n          = len(req.tasks)
    logger.info(
        "[server|batch] starting %d tasks in parallel  thread=%s",
        n, threading.current_thread().name,
    )

    def run_one(idx: int, task: BatchTask) -> dict:
        suffix = task.suffix or f"-p{idx + 1}"
        tname  = threading.current_thread().name
        t0     = time.monotonic()
        logger.info(
            "[server|batch] task %d/%d START  agent=%s suffix=%s  thread=%s",
            idx + 1, n, task.agent, suffix, tname,
        )
        bb.update_status(task.agent, "in_progress", increment_iteration=True)
        agent  = AGENT_MAP[task.agent](req.repo_path, force_deepseek=req.force_deepseek)
        result = agent.run(task.instruction, resume_session=False, output_suffix=suffix)

        final_status = "done" if result["status"] == "success" else "failed"
        bb.update_status(task.agent, final_status)

        val_passed, val_issues = validate_agent_output(task.agent, bb)
        if not val_passed:
            logger.warning(
                "[server|batch] task %d/%d agent=%s output incomplete: %s",
                idx + 1, n, task.agent, val_issues,
            )

        elapsed = time.monotonic() - t0
        logger.info(
            "[server|batch] task %d/%d END    agent=%s status=%s validation=%s elapsed=%.1fs  thread=%s",
            idx + 1, n, task.agent, final_status, "OK" if val_passed else "FAIL", elapsed, tname,
        )
        return {
            "index":      idx,
            "agent":      task.agent,
            "suffix":     suffix,
            "status":     final_status,
            "output":     result["output"],
            "validation": {"passed": val_passed, "issues": val_issues},
        }

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(run_one, i, task) for i, task in enumerate(req.tasks)]
        results = [f.result() for f in futures]

    wall_elapsed = time.monotonic() - wall_start
    succeeded    = sum(1 for r in results if r["status"] == "done")
    logger.info(
        "[server|batch] %d/%d tasks succeeded  wall=%.1fs",
        succeeded, n, wall_elapsed,
    )

    return {
        "batch_size": n,
        "wall_time":  round(wall_elapsed, 1),
        "results":    results,
    }


@app.get("/stream")
async def stream_events(repo_path: str = ""):
    """SSE endpoint — browsers connect here to receive live agent events."""
    q = bus.subscribe()

    async def generate():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    if (repo_path
                            and event.get("repo_path") not in ("", repo_path)
                            and event.get("agent") != "consultant"):
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class AgentActivateRequest(BaseModel):
    agent:       str
    repo_path:   str
    instruction: str


@app.post("/agent/activate")
def agent_activate(req: AgentActivateRequest):
    """Start an agent immediately in a background thread, outside the pipeline order.

    If the agent is already running, the instruction is queued in its inbox
    instead (it will pick it up at the next loop step).
    """
    if req.agent not in AGENT_MAP:
        raise HTTPException(400, f"Unknown agent '{req.agent}'")
    _require_repo(req.repo_path)

    if req.agent in interrupt_bus.active():
        result = interrupt_bus.send(req.agent, req.instruction)
        return {"agent": req.agent, "status": "queued", **result}

    bb = BlackBoard(req.repo_path)
    bb.update_status(req.agent, "in_progress", increment_iteration=True)

    def _run():
        try:
            agent  = AGENT_MAP[req.agent](req.repo_path)
            result = agent.run(req.instruction)
            status = "done" if result["status"] == "success" else "failed"
        except Exception:
            logger.exception("[activate] agent=%s crashed", req.agent)
            status = "failed"
        bb.update_status(req.agent, status)

    threading.Thread(target=_run, daemon=True, name=f"activate-{req.agent}").start()
    return {"agent": req.agent, "status": "started"}


class AgentPromptRequest(BaseModel):
    agent:   str
    message: str


@app.post("/agent/prompt")
def agent_prompt(req: AgentPromptRequest):
    """Inject a user message into a running agent's conversation mid-loop."""
    if req.agent not in AGENT_MAP:
        raise HTTPException(400, f"Unknown agent '{req.agent}'")
    result = interrupt_bus.send(req.agent, req.message)
    return {"agent": req.agent, **result}


@app.get("/agent/active")
def agent_active():
    """Return the list of agents that currently have an active interrupt queue."""
    return {"agents": interrupt_bus.active()}


@app.get("/blackboard/list")
def list_blackboard(repo_path: str):
    _require_repo(repo_path)
    bb_dir = Path(repo_path) / ".blackboard"
    if not bb_dir.exists():
        return {"files": []}
    files = sorted(
        str(f.relative_to(bb_dir)).replace("\\", "/")
        for f in bb_dir.rglob("*") if f.is_file()
    )
    return {"files": files}


@app.get("/blackboard")
def read_blackboard(repo_path: str, filename: str):
    _require_repo(repo_path)
    content = BlackBoard(repo_path).read(filename)
    return {"filename": filename, "content": content}


@app.post("/blackboard")
def write_blackboard(req: BlackboardWriteRequest):
    _require_repo(req.repo_path)
    BlackBoard(req.repo_path).write(req.filename, req.content, append=req.append)
    return {"status": "ok", "filename": req.filename}


@app.get("/status")
def get_status(repo_path: str):
    _require_repo(repo_path)
    return BlackBoard(repo_path).get_status()


# ── helpers ──────────────────────────────────────────────────────────────────

def _require_repo(path: str):
    if not Path(path).exists():
        raise HTTPException(400, f"Repo path does not exist: {path}")


def _ensure_repo(path: str, pipeline_id: str = "", repo_path: str = ""):
    """Create repo_path if missing, emitting a warning event so the user knows."""
    p = Path(path)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        if pipeline_id:
            _emit_pipeline_event("warning", pipeline_id, repo_path or path,
                                 f"Directory did not exist and was created: {path}")


# ── entry point ──────────────────────────────────────────────────────────────

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestration_server:app", host="127.0.0.1", port=8765, reload=True)
