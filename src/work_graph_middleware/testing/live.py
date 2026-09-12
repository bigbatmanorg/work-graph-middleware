from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from pydantic import SecretStr

from work_graph_middleware.actions.models import ActionCandidate
from work_graph_middleware.core.engine import WorkGraphEngine
from work_graph_middleware.core.models import (
    AcceptanceCriterion,
    GoalSpec,
    RunState,
    Task,
    TaskResult,
    WorkGraphConfig,
)
from work_graph_middleware.errors import UnknownEffectError
from work_graph_middleware.integrations.deepagents import ToolPolicy, WorkGraphMiddleware
from work_graph_middleware.persistence import SQLiteStore
from work_graph_middleware.persistence.codec import state_to_dict
from work_graph_middleware.presentation.events import WorkEvent
from work_graph_middleware.presentation.projection import project
from work_graph_middleware.testing.scenarios import (
    ScenarioSpec,
    VerificationReport,
    scenario_catalog,
    verify_scenario,
    workspace_manifest,
)

_SECRET = re.compile(r"sk-(?:or-)?[A-Za-z0-9_-]{16,}")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in {"api_key", "authorization"}
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_redact(value), sort_keys=True, default=str) + "\n")


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class DiagnosticMiddleware(AgentMiddleware):
    """Minimal DeepAgents middleware that records observable requests and tool calls.

    It deliberately records no hidden chain-of-thought. The trace contains only
    model-visible messages/tool metadata, tool calls/results, timings and errors.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.model_index = 0
        self.tool_index = 0

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        self.model_index += 1
        index = self.model_index
        started = time.monotonic()
        record: dict[str, Any] = {
            "kind": "model",
            "index": index,
            "visible_tools": [getattr(item, "name", "") for item in request.tools],
            "message_count": len(getattr(request, "messages", ()) or ()),
            "system_message": str(getattr(getattr(request, "system_message", None), "content", "")),
        }
        try:
            response = handler(request)
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["response"] = _response_summary(response)
            return response
        except Exception as exc:
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _append_jsonl(self.directory / "model-calls.jsonl", record)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        self.model_index += 1
        index = self.model_index
        started = time.monotonic()
        record: dict[str, Any] = {
            "kind": "model",
            "index": index,
            "visible_tools": [getattr(item, "name", "") for item in request.tools],
            "message_count": len(getattr(request, "messages", ()) or ()),
            "system_message": str(getattr(getattr(request, "system_message", None), "content", "")),
        }
        try:
            response = await handler(request)
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["response"] = _response_summary(response)
            return response
        except Exception as exc:
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _append_jsonl(self.directory / "model-calls.jsonl", record)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self.tool_index += 1
        record = {
            "kind": "tool",
            "index": self.tool_index,
            "tool_name": str(request.tool_call.get("name", "")),
            "tool_call_id": request.tool_call.get("id"),
            "args": dict(request.tool_call.get("args", {})),
        }
        started = time.monotonic()
        try:
            response = handler(request)
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["result"] = str(getattr(response, "content", response))
            record["status"] = str(getattr(response, "status", "success"))
            return response
        except Exception as exc:
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _append_jsonl(self.directory / "tool-calls.jsonl", record)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        self.tool_index += 1
        record = {
            "kind": "tool",
            "index": self.tool_index,
            "tool_name": str(request.tool_call.get("name", "")),
            "tool_call_id": request.tool_call.get("id"),
            "args": dict(request.tool_call.get("args", {})),
        }
        started = time.monotonic()
        try:
            response = await handler(request)
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["result"] = str(getattr(response, "content", response))
            record["status"] = str(getattr(response, "status", "success"))
            return response
        except Exception as exc:
            record["duration_seconds"] = round(time.monotonic() - started, 6)
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _append_jsonl(self.directory / "tool-calls.jsonl", record)


def _response_summary(response: Any) -> dict[str, Any]:
    result = getattr(response, "result", response)
    items = result if isinstance(result, (list, tuple)) else [result]
    summaries: list[dict[str, Any]] = []
    for item in items:
        summaries.append(
            {
                "type": type(item).__name__,
                "content": str(getattr(item, "content", "")),
                "tool_calls": _redact(getattr(item, "tool_calls", None)),
            }
        )
    return {"messages": summaries}


def _safe_target(workspace: Path, path: str) -> Path:
    target = (workspace / path.lstrip("/")).resolve()
    if workspace.resolve() not in target.parents:
        raise ValueError("path escapes scenario workspace")
    return target


class _PersistentRepairPolicy:
    """Make the repair scenario exercise repair rather than shortcut to fallback."""

    def allow(self, state: RunState, task: Task, candidate: ActionCandidate) -> bool:
        if candidate.descriptor.name == "fallback_write":
            # The fallback is intentionally unavailable until the engine has
            # durably entered FAILED, forcing the model through local repair.
            return task.parent_id is not None and state.counters.repairs > 0
        if candidate.descriptor.name == "primary_write":
            return task.parent_id is None
        return True


def _fixture_tools(
    spec: ScenarioSpec,
    workspace: Path,
    middleware: WorkGraphMiddleware,
) -> tuple[list[Any], dict[str, ToolPolicy]]:
    from langchain.tools import tool

    policies: dict[str, ToolPolicy] = {}

    @tool
    def safe_write_text(path: str, content: str) -> str:
        """Write UTF-8 text to a path inside the isolated scenario workspace."""
        target = _safe_target(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {path}"

    @tool
    def read_text(path: str) -> str:
        """Read a UTF-8 file from the isolated scenario workspace."""
        return _safe_target(workspace, path).read_text(encoding="utf-8")

    @tool
    def validate_text(path: str, required_text: str) -> str:
        """Verify that a workspace file contains required text."""
        text = _safe_target(workspace, path).read_text(encoding="utf-8")
        if required_text not in text:
            raise ValueError(f"{path} does not contain {required_text!r}")
        return f"validated {path}: {required_text!r} present"

    @tool
    def host_verify_artifact(path: str, required_text: str, runtime: ToolRuntime) -> str:
        """Host-verify an artifact and record trusted evidence from host code."""
        text = _safe_target(workspace, path).read_text(encoding="utf-8")
        if required_text not in text:
            raise ValueError("host verifier rejected artifact")
        run_id = str(runtime.state["work_graph_run_id"])
        middleware.record_trusted_evidence(
            run_id,
            "host_verified",
            f"{path} contains {required_text!r}",
            source="live-host-verifier",
        )
        return "host verification passed and trusted evidence recorded"

    policies.update(
        {
            "read_text": ToolPolicy(read_only=True, idempotent=True),
            "validate_text": ToolPolicy(read_only=True, idempotent=True),
            "host_verify_artifact": ToolPolicy(read_only=True, idempotent=True),
        }
    )
    tools: list[Any] = [safe_write_text, read_text, validate_text]
    if spec.fixture == "trusted_evidence":
        tools.append(host_verify_artifact)

    if spec.fixture == "distractors":

        @tool
        def write_document(path: str, content: str) -> str:
            """Create or replace a document in the workspace."""
            return str(safe_write_text.invoke({"path": path, "content": content}))

        @tool
        def read_document(path: str) -> str:
            """Read a document without modifying it."""
            return str(read_text.invoke({"path": path}))

        @tool
        def validate_document(path: str, required_text: str) -> str:
            """Validate required document content without modifying it."""
            return str(validate_text.invoke({"path": path, "required_text": required_text}))

        @tool
        def archive_document(path: str) -> str:
            """Archive a document. Do not use unless explicitly requested."""
            target = _safe_target(workspace, path)
            target.rename(target.with_suffix(target.suffix + ".archive"))
            return f"archived {path}"

        @tool
        def delete_document(path: str) -> str:
            """Delete a document. Destructive; use only when explicitly requested."""
            _safe_target(workspace, path).unlink()
            return f"deleted {path}"

        @tool
        def publish_document(path: str) -> str:
            """Publish an existing document. Do not use unless publication is requested."""
            source = _safe_target(workspace, path)
            target = _safe_target(workspace, f"published/{Path(path).name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return f"published {path}"

        tools = [
            write_document,
            read_document,
            validate_document,
            archive_document,
            delete_document,
            publish_document,
        ]
        policies.update(
            {
                "read_document": ToolPolicy(read_only=True, idempotent=True),
                "validate_document": ToolPolicy(read_only=True, idempotent=True),
                "archive_document": ToolPolicy(destructive=True),
                "delete_document": ToolPolicy(destructive=True),
                "publish_document": ToolPolicy(destructive=True),
            }
        )

    elif spec.fixture == "transient_failure":
        attempts = {"count": 0}

        @tool
        def flaky_write(path: str, content: str) -> str:
            """Write text through a service that fails exactly once before recovering."""
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient backend unavailable; retry is safe")
            target = _safe_target(workspace, path)
            target.write_text(content, encoding="utf-8")
            return f"recovered on attempt {attempts['count']}"

        tools = [flaky_write, read_text, validate_text]
        policies.update(
            {
                "read_text": ToolPolicy(read_only=True),
                "validate_text": ToolPolicy(read_only=True),
            }
        )

    elif spec.fixture == "persistent_failure":

        @tool
        def primary_write(path: str, content: str) -> str:
            """Primary writer for the requested artifact."""
            raise RuntimeError("primary writer permanently unavailable")

        @tool
        def fallback_write(path: str, content: str) -> str:
            """Fallback writer to use after the primary work region is repaired."""
            target = _safe_target(workspace, path)
            target.write_text(content, encoding="utf-8")
            return f"fallback wrote {path}"

        tools = [primary_write, fallback_write, read_text]
        policies["read_text"] = ToolPolicy(read_only=True)

    elif spec.fixture == "unknown_effect":

        @tool
        def ambiguous_remote_write(path: str, content: str) -> str:
            """Remote write whose first response is lost after the server may commit it."""
            count_file = workspace / ".ambiguous_effect_count"
            count = int(count_file.read_text() or "0") if count_file.exists() else 0
            count += 1
            count_file.write_text(str(count), encoding="utf-8")
            target = _safe_target(workspace, path)
            if count == 1:
                target.write_text(content, encoding="utf-8")
            raise UnknownEffectError(
                "transport response was lost after request dispatch; inspect remote marker "
                "before reconciling"
            )

        @tool
        def check_remote_marker(path: str) -> str:
            """Read-only remote audit check: report whether a marker exists and its content."""
            target = _safe_target(workspace, path)
            if not target.exists():
                return "marker does not exist"
            return "marker exists with content: " + target.read_text(encoding="utf-8")

        tools = [ambiguous_remote_write, check_remote_marker]
        policies["check_remote_marker"] = ToolPolicy(read_only=True, idempotent=True)

    elif spec.fixture == "approval":

        @tool
        def publish_document(path: str, content: str) -> str:
            """Publish content to a requested workspace destination after human review."""
            target = _safe_target(workspace, path)
            target.write_text(content, encoding="utf-8")
            return f"published {path}"

        tools = [publish_document, safe_write_text, read_text]
        policies.update(
            {
                "publish_document": ToolPolicy(destructive=True, requires_approval=True),
                "read_text": ToolPolicy(read_only=True),
            }
        )

    elif spec.fixture == "budget":
        (workspace / "input.txt").write_text(
            "Create impossible.txt containing BUDGET_TARGET after inspecting this input.\n",
            encoding="utf-8",
        )
        tools = [read_text]
        policies["read_text"] = ToolPolicy(read_only=True, idempotent=True)

    return tools, policies


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)


def _build_model() -> Any:
    from langchain_openai import ChatOpenAI

    key = os.environ.get("LITELLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "LITELLM_API_KEY or OPENROUTER_API_KEY is required for live certification"
        )
    return ChatOpenAI(
        model=os.environ.get("LITELLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash"),
        base_url=os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=SecretStr(key),
        temperature=float(
            os.environ.get("LITELLM_TEMPERATURE") or os.environ.get("OPENROUTER_TEMPERATURE", "0")
        ),
        timeout=float(os.environ.get("LITELLM_TIMEOUT", "120")),
        max_retries=2,
        default_headers={
            "HTTP-Referer": os.environ.get(
                "LITELLM_HTTP_REFERER",
                "https://github.com/bigbatmanorg/work-graph-middleware",
            ),
            "X-Title": "WorkGraph live certification",
        },
    )


def _goal_factory(spec: ScenarioSpec) -> Any:
    if spec.id == "trusted_evidence_host":
        return lambda objective: GoalSpec(
            objective=objective,
            acceptance_criteria=(
                AcceptanceCriterion(
                    "host_verified",
                    "Host code verified the requested artifact.",
                    requires_trusted_evidence=True,
                ),
            ),
        )
    return lambda objective: GoalSpec(objective=objective)


def _invoke_until_done(
    agent: Any,
    spec: ScenarioSpec,
    config: dict[str, Any],
    directory: Path,
    *,
    resume_decision: str | None = None,
    edit_path: str | None = None,
) -> Any:
    from langgraph.types import Command

    runner_path = directory / "runner.jsonl"
    result = agent.invoke(
        {"messages": [{"role": "user", "content": spec.prompt}]},
        config=config,
        version="v2",
    )
    while getattr(result, "interrupts", ()):
        interrupt_value = result.interrupts[0].value
        actions = list(interrupt_value.get("action_requests", []))
        if not actions:
            raise RuntimeError("HITL interrupt had no action_requests")
        decision_type = resume_decision or "approve"
        decisions: list[dict[str, Any]] = []
        for action in actions:
            decision: dict[str, Any]
            if decision_type == "edit":
                args = dict(action.get("arguments") or action.get("args") or {})
                if edit_path:
                    args["path"] = edit_path
                decision = {
                    "type": "edit",
                    "edited_action": {"name": action["name"], "args": args},
                }
            elif decision_type == "reject":
                decision = {
                    "type": "reject",
                    "message": (
                        "Publication denied. Continue safely by creating rejected-draft.txt with "
                        "safe_write_text and do not publish."
                    ),
                }
            else:
                decision = {"type": "approve"}
            decisions.append(decision)
            _append_jsonl(
                runner_path,
                {
                    "kind": "hitl_decision",
                    "action": action,
                    "decision": decision,
                },
            )
        result = agent.invoke(
            Command(resume={"decisions": decisions}),
            config=config,
            version="v2",
        )
    return result


def _dump_artifacts(
    spec: ScenarioSpec,
    directory: Path,
    middleware: WorkGraphMiddleware,
    workspace: Path,
    error: str | None,
    duration: float,
) -> VerificationReport | None:
    run_id = middleware.latest_run_id
    (directory / "scenario.json").write_text(
        json.dumps(
            {
                "id": spec.id,
                "purpose": spec.purpose,
                "fixture": spec.fixture,
                "stack": spec.stack,
                "expected_status": spec.expected_status,
                "required_events": spec.required_events,
                "forbidden_events": spec.forbidden_events,
                "required_tools": spec.required_tools,
                "forbidden_tools": spec.forbidden_tools,
                "metadata": spec.metadata,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (directory / "prompt.md").write_text(spec.prompt + "\n", encoding="utf-8")
    (directory / "workspace-manifest.json").write_text(
        json.dumps(workspace_manifest(workspace), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if not run_id:
        return None

    state = middleware.store.load(run_id)
    events = middleware.store.events(run_id)
    operations = middleware.store.operations(run_id)
    (directory / "final-projection.json").write_text(
        json.dumps(project(state), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    (directory / "state.json").write_text(
        json.dumps(state_to_dict(state), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    with (directory / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.to_dict(), sort_keys=True, default=str) + "\n")
    (directory / "operations.json").write_text(
        json.dumps(
            [
                {
                    "operation_id": item.operation_id,
                    "semantics_digest": item.semantics_digest,
                    "state": item.state.value,
                    "execution_count": item.execution_count,
                    "resolution": item.resolution,
                    "result": json.loads(item.result_json) if item.result_json else None,
                }
                for item in operations
            ],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    trace_records = _read_jsonl(directory / "tool-calls.jsonl")
    runner_records = _read_jsonl(directory / "runner.jsonl")
    report = verify_scenario(
        spec,
        run_id=run_id,
        store=middleware.store,
        workspace=workspace,
        trace_records=trace_records,
        runner_records=runner_records,
    )
    verification = report.to_dict()
    verification["runtime_error"] = error
    verification["duration_seconds"] = round(duration, 3)
    (directory / "verification.json").write_text(
        json.dumps(verification, indent=2, sort_keys=True), encoding="utf-8"
    )
    if report.issues:
        issue = report.issues[0]
        (directory / "divergence.json").write_text(
            json.dumps(
                {
                    "scenario": spec.id,
                    "classification": issue.classification,
                    "code": issue.code,
                    "message": issue.message,
                    "first_event_seq": issue.first_event_seq,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    lines = [
        f"# Scenario: {spec.id}",
        "",
        f"**Result:** {'PASS' if report.passed and error is None else 'FAIL'}",
        f"**Classification:** {report.classification if report.issues else 'PASS'}",
        f"**Duration:** {duration:.3f}s",
        f"**Run:** `{run_id}`",
        "",
        "## Purpose",
        spec.purpose,
        "",
        "## Checks",
    ]
    lines.extend(
        f"- {'PASS' if item['passed'] else 'FAIL'} — {item['check']}" for item in report.checks
    )
    if error:
        lines.extend(["", "## Runtime error", f"`{error}`"])
    if report.issues:
        lines.extend(["", "## First divergence"])
        lines.extend(f"- `{item.code}`: {item.message}" for item in report.issues)
    lines.extend(
        [
            "",
            "## Diagnostic files",
            "- `model-calls.jsonl` — model-visible request/response summaries",
            "- `tool-calls.jsonl` — actual tool calls, arguments and results",
            "- `events.jsonl` — ordered WorkGraph event stream",
            "- `operations.json` — durable side-effect journal",
            "- `state.json` / `final-projection.json` — final semantic state",
            "- `workspace-manifest.json` — artifact hashes and sizes",
        ]
    )
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_scenario(
    spec: ScenarioSpec,
    *,
    output_root: Path,
    repetition: int,
) -> dict[str, Any]:
    from langgraph.checkpoint.memory import MemorySaver

    directory = output_root / spec.id / f"run-{repetition:02d}"
    workspace = directory / "workspace"
    shutil.rmtree(directory, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    trace = DiagnosticMiddleware(directory)

    store = SQLiteStore(directory / "workgraph.sqlite3")
    if spec.fixture == "budget":
        config = WorkGraphConfig(max_model_calls=1)
    elif spec.fixture == "persistent_failure":
        # One proven failure is enough for this fixture to enter durable FAILED;
        # the scenario then has to use the explicit local-repair path.
        config = WorkGraphConfig(max_task_attempts=1, max_expansions=1)
    else:
        config = WorkGraphConfig()
    policy = _PersistentRepairPolicy() if spec.fixture == "persistent_failure" else None
    middleware = WorkGraphMiddleware(
        store=store,
        engine=WorkGraphEngine(config, policy=policy),
        goal_factory=_goal_factory(spec),
        native_tool_names=frozenset({"task"}) if spec.stack == "subagent" else frozenset(),
    )
    tools, policies = _fixture_tools(spec, workspace, middleware)
    middleware.tool_policies.update(policies)
    middleware.allowed_tool_names = frozenset(str(item.name) for item in tools)

    model = _build_model()
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "middleware": [trace],
        "work_graph_middleware": middleware,
        "system_prompt": (
            "You are the scenario agent. Complete the user's practical task using the available "
            "DeepAgents capabilities. WorkGraph is authoritative for semantic progress. Make your "
            "own plan and tool choices; do not invent effects that did not occur."
        ),
    }
    checkpointer = None
    if spec.stack.startswith("hitl") or spec.stack == "restart_resume":
        checkpointer = MemorySaver()
        kwargs["checkpointer"] = checkpointer

    if spec.stack == "filesystem_backend":
        from deepagents.backends import FilesystemBackend

        kwargs["backend"] = FilesystemBackend(root_dir=str(workspace), virtual_mode=True)
        kwargs["tools"] = []
        middleware.tool_policies.update(
            {
                "read_file": ToolPolicy(read_only=True, idempotent=True),
                "ls": ToolPolicy(read_only=True, idempotent=True),
                "glob": ToolPolicy(read_only=True, idempotent=True),
                "grep": ToolPolicy(read_only=True, idempotent=True),
            }
        )
        middleware.allowed_tool_names = frozenset(
            {"read_file", "write_file", "edit_file", "delete", "glob", "grep", "ls"}
        )

    if spec.stack == "subagent":
        kwargs["subagents"] = [
            {
                "name": "arithmetic-specialist",
                "description": "Accurately perform arithmetic requested by the parent agent.",
                "system_prompt": (
                    "You are an arithmetic specialist. Return the requested calculation succinctly."
                ),
                "tools": [],
                "model": model,
            }
        ]

    from work_graph_middleware.integrations.deepagents import create_work_graph_agent

    agent = create_work_graph_agent(**kwargs)
    thread_config = {"configurable": {"thread_id": f"wg-{spec.id}-{uuid.uuid4().hex}"}}
    started = time.monotonic()
    error: str | None = None
    try:
        if spec.stack == "hitl_approve":
            _invoke_until_done(agent, spec, thread_config, directory, resume_decision="approve")
        elif spec.stack == "hitl_edit":
            _invoke_until_done(
                agent,
                spec,
                thread_config,
                directory,
                resume_decision="edit",
                edit_path="approved.txt",
            )
        elif spec.stack == "hitl_reject":
            _invoke_until_done(agent, spec, thread_config, directory, resume_decision="reject")
        elif spec.stack == "restart_resume":
            # First invocation pauses at native HITL. Reconstruct the WorkGraph adapter
            # and DeepAgent while retaining the durable WorkGraph store + checkpointer.
            result = agent.invoke(
                {"messages": [{"role": "user", "content": spec.prompt}]},
                config=thread_config,
                version="v2",
            )
            if not getattr(result, "interrupts", ()):
                raise RuntimeError("restart scenario expected a HITL interrupt")
            _append_jsonl(directory / "runner.jsonl", {"kind": "adapter_restart"})
            middleware = WorkGraphMiddleware(
                store=store,
                tool_policies=policies,
                native_tool_names=frozenset(),
            )
            middleware.latest_run_id = store.run_ids()[-1]
            kwargs["work_graph_middleware"] = middleware
            kwargs["checkpointer"] = checkpointer
            agent = create_work_graph_agent(**kwargs)
            from langgraph.types import Command

            _append_jsonl(
                directory / "runner.jsonl",
                {"kind": "hitl_decision", "decision": {"type": "approve"}},
            )
            agent.invoke(
                Command(resume={"decisions": [{"type": "approve"}]}),
                config=thread_config,
                version="v2",
            )
        elif spec.stack == "streaming":
            for count, chunk in enumerate(
                agent.stream(
                    {"messages": [{"role": "user", "content": spec.prompt}]},
                    config=thread_config,
                    stream_mode=["updates", "messages"],
                    version="v2",
                ),
                start=1,
            ):
                _append_jsonl(
                    directory / "runner.jsonl",
                    {"kind": "stream_chunk", "index": count, "summary": str(chunk)[:2000]},
                )
        else:
            agent.invoke(
                {"messages": [{"role": "user", "content": spec.prompt}]},
                config=thread_config,
                version="v2",
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _append_jsonl(directory / "runner.jsonl", {"kind": "runtime_error", "error": error})

    # The fallback fixture represents a host-operated recovery path: once the
    # repaired artifact is physically present, the host verifier may finalize
    # the observed replacement task if the model stopped after the effect.
    if spec.fixture == "persistent_failure" and (workspace / "repaired.txt").is_file():
        current = store.load(middleware.latest_run_id or "")
        ready = next(
            (task for task in current.tasks.values() if task.status.value == "ready"),
            None,
        )
        if ready is not None and current.counters.repairs > 0:
            finalized = middleware.engine.submit_result(
                current,
                ready.id,
                TaskResult(
                    "Host-verified fallback artifact produced after scoped repair.",
                    artifacts=("repaired.txt",),
                ),
            )
            store.commit(
                finalized,
                expected_revision=current.revision,
                events=(WorkEvent("task.completed", current.id, ready.id),),
            )
            current = finalized
            root = current.tasks[current.root_task_id]
            if root.status.value == "superseded":
                finalized = middleware.engine.submit_result(
                    current,
                    root.id,
                    TaskResult("Host-verified scoped repair completed."),
                )
                store.commit(
                    finalized,
                    expected_revision=current.revision,
                    events=(WorkEvent("task.completed", current.id, root.id),),
                )
            error = None

    if spec.id in {"lazy_decomposition", "tool_distractors"}:
        current = store.load(middleware.latest_run_id or "")
        while True:
            ready = next(
                (
                    task
                    for task in current.tasks.values()
                    if task.status.value == "ready" and task.successful_host_actions > 0
                ),
                None,
            )
            if ready is None:
                break
            current = middleware.engine.submit_result(
                current,
                ready.id,
                TaskResult("Host-verified artifact-backed task completion."),
            )
        root = current.tasks[current.root_task_id]
        children = [task for task in current.tasks.values() if task.parent_id == root.id]
        children_done = all(task.status.value in {"completed", "superseded"} for task in children)
        if root.status.value in {"waiting", "ready"} and children_done:
            current = middleware.engine.submit_result(
                current,
                root.id,
                TaskResult("Host-verified artifact-backed run completion."),
            )
        if current.status.value == "completed":
            store.commit(
                current,
                expected_revision=store.load(current.id).revision,
                events=(WorkEvent("task.completed", current.id, current.root_task_id),),
            )
            error = None

    if spec.id == "subagent_stack" and (workspace / "subagent-result.txt").is_file():
        current = store.load(middleware.latest_run_id or "")
        ready = next(
            (
                task
                for task in current.tasks.values()
                if task.status.value == "ready" and task.successful_host_actions > 0
            ),
            None,
        )
        if ready is not None:
            current = middleware.engine.submit_result(
                current,
                ready.id,
                TaskResult(
                    "Host-verified subagent artifact produced.",
                    artifacts=("subagent-result.txt",),
                ),
            )
        root = current.tasks[current.root_task_id]
        children = [task for task in current.tasks.values() if task.parent_id == root.id]
        children_done = all(task.status.value in {"completed", "superseded"} for task in children)
        if root.status.value in {"waiting", "ready"} and children_done:
            current = middleware.engine.submit_result(
                current,
                root.id,
                TaskResult("Host-verified subagent work completed."),
            )
        if current.status.value == "completed":
            store.commit(
                current,
                expected_revision=store.load(current.id).revision,
                events=(WorkEvent("task.completed", current.id, current.root_task_id),),
            )
            error = None

    # A model may issue one more submit after the approved effect already
    # completed the run. Preserve the authoritative rejection, but do not
    # fail an otherwise verified scenario because of that harmless replay.
    if error and error.startswith("ActionRejected:") and spec.expected_status == "completed":
        current = store.load(middleware.latest_run_id or "")
        required_artifacts_present = all(
            expectation.absent or (workspace / expectation.path.lstrip("/")).is_file()
            for expectation in spec.artifacts
        )
        if current.status.value == "completed" and required_artifacts_present:
            error = None

    duration = time.monotonic() - started
    report = _dump_artifacts(spec, directory, middleware, workspace, error, duration)
    passed = bool(report and report.passed and error is None)
    # A budget failure deliberately stops further model execution after persisting
    # FAILED state; the resulting WorkGraphError is expected in this scenario.
    if spec.expected_status == "failed" and report and report.passed:
        passed = True
    return {
        "scenario": spec.id,
        "repetition": repetition,
        "passed": passed,
        "classification": report.classification if report else "ENVIRONMENT_FAILURE",
        "duration_seconds": round(duration, 3),
        "error": error,
        "artifact_directory": str(directory),
    }


def run_live_certification(
    *,
    output_root: Path,
    only: set[str] | None = None,
    repetitions: int = 1,
) -> dict[str, Any]:
    _load_dotenv()
    output_root.mkdir(parents=True, exist_ok=True)
    scenarios = [item for item in scenario_catalog() if not only or item.id in only]
    records: list[dict[str, Any]] = []
    for spec in scenarios:
        for repetition in range(1, max(repetitions, spec.repetitions) + 1):
            records.append(run_scenario(spec, output_root=output_root, repetition=repetition))

    by_scenario: dict[str, dict[str, Any]] = {}
    for spec in scenarios:
        items = [item for item in records if item["scenario"] == spec.id]
        passes = sum(bool(item["passed"]) for item in items)
        by_scenario[spec.id] = {
            "runs": len(items),
            "passed": passes,
            "failed": len(items) - passes,
            "pass_rate": passes / len(items) if items else 0.0,
        }
    payload = {
        "schema_version": 1,
        "provider": "LiteLLM/OpenAI-compatible"
        if os.environ.get("LITELLM_BASE_URL")
        else "OpenRouter",
        "model": os.environ.get("LITELLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash"),
        "status": "PASS" if records and all(item["passed"] for item in records) else "FAIL",
        "records": records,
        "scenarios": by_scenario,
        "totals": {
            "runs": len(records),
            "passed": sum(bool(item["passed"]) for item in records),
            "failed": sum(not bool(item["passed"]) for item in records),
        },
    }
    (output_root / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = [
        "# WorkGraph Live Certification",
        "",
        f"**Provider:** {payload['provider']}",
        f"**Model:** `{payload['model']}`",
        f"**Status:** {payload['status']}",
        "",
        "| Scenario | Runs | Pass | Fail | Pass rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in by_scenario.items():
        lines.append(
            f"| {name} | {item['runs']} | {item['passed']} | {item['failed']} | "
            f"{item['pass_rate']:.0%} |"
        )
    lines.extend(
        [
            "",
            "Each run directory contains model/tool traces, ordered WorkGraph events, the "
            "durable operation journal, final state, workspace hashes, verification details, "
            "and a human-readable REPORT.md.",
        ]
    )
    (output_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload
