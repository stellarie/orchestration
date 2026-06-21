import asyncio
import json
import logging
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from logging_config import setup_logging
from blackboard import BlackBoard
from config import AGENT_CONCURRENCY, ANTHROPIC_API_KEY
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
    repo_path: str
    tasks:     list[BatchTask]


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


PIPELINE_ORDER = [
    "architect", "designer", "planner", "scaffolder", "tester", "reviewer",
    "test-generator", "coder", "qa-tester", "code-reviewer", "commit", "documentation",
]


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "anthropic_available": bool(ANTHROPIC_API_KEY)}


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
    """Start the full 12-agent pipeline in a background thread.

    All agents run with force_deepseek=True (full-auto mode).
    Returns immediately; progress arrives via /stream SSE.
    """
    _require_repo(req.repo_path)
    task_id = str(uuid.uuid4())[:8]
    bb      = BlackBoard(req.repo_path)
    bb.init_task(task_id, req.description)
    bb.write("run-id", task_id)

    instr = (
        "Complete your pipeline role. "
        "The task is described in .blackboard/task.md. "
        "Read all relevant blackboard files from previous pipeline steps before acting."
    )

    def _run_pipeline():
        for agent_name in PIPELINE_ORDER:
            try:
                bb.update_status(agent_name, "in_progress", increment_iteration=True)
                agent  = AGENT_MAP[agent_name](req.repo_path, force_deepseek=True)
                result = agent.run(instr)
                status = "done" if result["status"] == "success" else "failed"
            except Exception:
                logger.exception("[pipeline] agent=%s crashed", agent_name)
                status = "failed"
            bb.update_status(agent_name, status)
            if status == "failed":
                logger.warning("[pipeline] stopping at agent=%s", agent_name)
                break

    threading.Thread(target=_run_pipeline, daemon=True, name=f"pipeline-{task_id}").start()
    return {"task_id": task_id, "status": "started"}


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
        agent  = AGENT_MAP[task.agent](req.repo_path)
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
                    if repo_path and event.get("repo_path") not in ("", repo_path):
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


class AgentPromptRequest(BaseModel):
    agent:   str
    message: str


@app.post("/agent/prompt")
def agent_prompt(req: AgentPromptRequest):
    """Inject a user message into a running agent's conversation mid-loop."""
    if req.agent not in AGENT_MAP:
        raise HTTPException(400, f"Unknown agent '{req.agent}'")
    queued = interrupt_bus.send(req.agent, req.message)
    return {"agent": req.agent, "queued": queued}


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


# ── entry point ──────────────────────────────────────────────────────────────

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestration_server:app", host="127.0.0.1", port=8765, reload=True)
