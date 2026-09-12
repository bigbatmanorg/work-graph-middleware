"""First-class Deep Agents integration.

WorkGraph owns semantic work state and durable effect accounting. Deep Agents owns
agent execution, checkpointing, interrupts, subagents, filesystem backends and
conversation state. This adapter uses only public LangChain middleware hooks.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired, cast

from pydantic import AliasChoices, BaseModel, Field

from work_graph_middleware.actions.execution import operation_semantics
from work_graph_middleware.actions.models import ActionDescriptor, ActionReceipt, ActionSelection
from work_graph_middleware.core.engine import (
    Completed,
    ExecuteAction,
    Failed,
    NeedModelAction,
    NeedReconciliation,
    WorkGraphEngine,
)
from work_graph_middleware.core.graph import ExpansionProposal, GraphPatch, TaskSpec
from work_graph_middleware.core.models import (
    EffectStatus,
    EvidenceRecord,
    GoalSpec,
    RunStatus,
    TaskResult,
)
from work_graph_middleware.errors import (
    ActionRejected,
    RevisionConflict,
    UnknownEffectError,
    WorkGraphError,
)
from work_graph_middleware.persistence import MemoryStore, OperationState, WorkGraphStore
from work_graph_middleware.presentation.events import WorkEvent
from work_graph_middleware.presentation.projection import model_context, project

CONTROL_TOOL_NAMES = frozenset(
    {
        "work_graph_status",
        "work_graph_expand",
        "work_graph_submit_result",
        "work_graph_repair",
        "work_graph_retry",
        "work_graph_reconcile",
    }
)

SYSTEM_PROMPT = """\
WorkGraph is the authoritative semantic work-control layer for this run.
Do the real work with the available domain tools. Decompose only when useful.
When the objective explicitly requires separately tracked stages or dependencies,
represent those stages in WorkGraph before doing their domain work.
Use WorkGraph control tools to expand, submit, repair, retry, or reconcile when the
current state requires it. Never claim completion only in prose: submit actual task
results after real effects occur. Do not fabricate trusted evidence or external effects.
"""


def integration_available() -> bool:
    try:
        import deepagents  # noqa: F401
        import langchain  # noqa: F401
    except ImportError:
        return False
    return True


if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain.messages import SystemMessage, ToolMessage
    from langchain.tools import ToolRuntime, tool
else:
    try:
        from langchain.agents.middleware import AgentMiddleware, AgentState
        from langchain.messages import SystemMessage, ToolMessage
        from langchain.tools import ToolRuntime, tool
    except ImportError:

        class AgentMiddleware:  # type: ignore[no-redef]
            pass

        class AgentState(dict):  # type: ignore[no-redef]
            pass

        ToolMessage = Any  # type: ignore[assignment,misc]
        ToolRuntime = Any  # type: ignore[assignment,misc]
        NotRequired = Any  # type: ignore[assignment,misc]
        SystemMessage = Any  # type: ignore[assignment,misc]
        tool = None


class WorkGraphAgentState(AgentState):
    work_graph_run_id: NotRequired[str]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    requires_approval: bool = False
    fixed_arguments: dict[str, Any] | None = None


class ExpansionChild(BaseModel):
    """One child proposed by the model for a lazy graph expansion."""

    key: str = Field(
        validation_alias=AliasChoices("key", "id"),
        description="Unique local key used by depends_on entries.",
    )
    title: str
    description: str = ""
    depends_on: list[str] = Field(
        default_factory=list,
        description="Local child keys that must complete before this child starts.",
    )
    covers_requirements: list[str] = Field(default_factory=list)
    priority: int = 0


class WorkGraphMiddleware(AgentMiddleware):
    """Deep Agents middleware backed by a deterministic WorkGraph engine."""

    state_schema = WorkGraphAgentState

    def __init__(
        self,
        *,
        store: WorkGraphStore | None = None,
        engine: WorkGraphEngine | None = None,
        tool_policies: dict[str, ToolPolicy] | None = None,
        allowed_tool_names: frozenset[str] | None = None,
        native_tool_names: frozenset[str] = frozenset(),
        goal_factory: Callable[[str], GoalSpec] | None = None,
        inject_system_prompt: bool = True,
    ) -> None:
        if not integration_available():
            raise ImportError(
                "Install work-graph-middleware[deepagents] to use WorkGraphMiddleware."
            )
        self.store = store or MemoryStore()
        self.engine = engine or WorkGraphEngine()
        self.tool_policies = tool_policies or {}
        self.allowed_tool_names = allowed_tool_names
        self.native_tool_names = native_tool_names
        self.goal_factory = goal_factory or (lambda objective: GoalSpec(objective=objective))
        self.inject_system_prompt = inject_system_prompt
        self.latest_run_id: str | None = None  # diagnostics only; never semantic authority
        self.tools = self._build_control_tools()

    @staticmethod
    def _objective_from_state(state: Any) -> str:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        if not messages:
            return "WorkGraph task"
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") in {"user", "human"}:
                return str(message.get("content") or "WorkGraph task")
            content = getattr(message, "content", None)
            if content and type(message).__name__.lower().startswith("human"):
                return str(content)
        last = messages[-1]
        return (
            str(last.get("content", "WorkGraph task"))
            if isinstance(last, dict)
            else str(getattr(last, "content", "WorkGraph task"))
        )

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        run_id = state.get("work_graph_run_id") if isinstance(state, dict) else None
        if run_id:
            self.store.load(str(run_id))
            self.latest_run_id = str(run_id)
            return None
        run = self.engine.create_run(self.goal_factory(self._objective_from_state(state)))
        self.store.create(run, (WorkEvent("run.created", run.id),))
        self.latest_run_id = run.id
        return {"work_graph_run_id": run.id}

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    @staticmethod
    def _run_id(state: Any) -> str:
        run_id = state.get("work_graph_run_id") if isinstance(state, dict) else None
        if not run_id:
            raise WorkGraphError("WG.INTEGRATION.NO_RUN", "WorkGraph run id is missing")
        return str(run_id)

    def _descriptor(self, tool_obj: Any) -> ActionDescriptor:
        name = str(getattr(tool_obj, "name", "unknown"))
        policy = self.tool_policies.get(name, ToolPolicy())
        schema: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            schema = cast(dict[str, Any], tool_obj.get_input_schema().model_json_schema())
        return ActionDescriptor(
            name=name,
            description=str(getattr(tool_obj, "description", "")),
            args_schema=schema,
            read_only=policy.read_only,
            destructive=policy.destructive,
            idempotent=policy.idempotent,
            requires_approval=policy.requires_approval,
            fixed_arguments=dict(policy.fixed_arguments or {}),
        )

    def _domain_tools(self, tools: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(
            item
            for item in tools
            if getattr(item, "name", "") not in CONTROL_TOOL_NAMES
            and getattr(item, "name", "") not in self.native_tool_names
            and (
                self.allowed_tool_names is None
                or getattr(item, "name", "") in self.allowed_tool_names
            )
        )

    def _descriptors(self, tools: Sequence[Any]) -> tuple[ActionDescriptor, ...]:
        return tuple(self._descriptor(item) for item in self._domain_tools(tools))

    def _persist_failure(self, run_id: str, directive: Failed) -> None:
        state = self.store.load(run_id)
        if state.status == RunStatus.FAILED and state.failure_code == directive.code:
            return
        updated = self.engine.mark_failed(state, directive.code, task_id=directive.task_id)
        self.store.commit(
            updated,
            expected_revision=state.revision,
            events=(WorkEvent("run.failed", run_id, data={"code": directive.code}),),
        )

    def _visible_tools(self, directive: Any, tools: Sequence[Any]) -> list[Any]:
        controls = {name for name in CONTROL_TOOL_NAMES}
        if isinstance(directive, NeedModelAction):
            allowed = {item.descriptor.name for item in directive.context.actions} | {
                "work_graph_status",
                "work_graph_expand",
                "work_graph_submit_result",
            }
        elif isinstance(directive, NeedReconciliation):
            read_only = {
                descriptor.name for descriptor in self._descriptors(tools) if descriptor.read_only
            }
            allowed = {"work_graph_status", "work_graph_reconcile"} | read_only
        elif isinstance(directive, Failed):
            allowed = {"work_graph_status", "work_graph_retry", "work_graph_repair"}
        elif isinstance(directive, Completed):
            allowed = {"work_graph_status"}
        else:
            allowed = controls
        return [
            item
            for item in tools
            if getattr(item, "name", "") in allowed
            or (
                isinstance(directive, NeedModelAction)
                and getattr(item, "name", "") in self.native_tool_names
            )
        ]

    def _record_model_response(self, run_id: str) -> None:
        for _ in range(2):
            state = self.store.load(run_id)
            try:
                self.store.commit(
                    state.evolve(),
                    expected_revision=state.revision,
                    events=(WorkEvent("model.response", run_id),),
                )
                return
            except RevisionConflict:
                continue

    def _system_message(self, existing: Any, run_id: str, directive: Any) -> Any:
        if not self.inject_system_prompt:
            return existing
        compact = json.dumps(
            model_context(self.store.load(run_id), directive),
            sort_keys=True,
            separators=(",", ":"),
        )
        suffix = f"\n\n{SYSTEM_PROMPT}\nWorkGraph context: {compact}"
        content = getattr(existing, "content", "") if existing is not None else ""
        if isinstance(content, str):
            return SystemMessage(content=content + suffix)
        blocks = list(getattr(existing, "content_blocks", [])) if existing is not None else []
        return SystemMessage(content=[*blocks, {"type": "text", "text": suffix}])

    def _prepare_model_request(self, request: Any) -> tuple[Any, str]:
        run_id = self._run_id(request.state)
        state = self.store.load(run_id)
        directive = self.engine.next(state, self._descriptors(request.tools))
        if isinstance(directive, Failed) and state.status != RunStatus.FAILED:
            self._persist_failure(run_id, directive)
            state = self.store.load(run_id)
            directive = self.engine.next(state, self._descriptors(request.tools))

        if isinstance(directive, Failed) and directive.code == "WG.BUDGET.MODEL_CALLS":
            raise WorkGraphError(
                "WG.BUDGET.MODEL_CALLS",
                "Model-call budget exhausted; run state was durably failed",
            )

        visible = self._visible_tools(directive, request.tools)
        # A final natural-language response after WorkGraph completion is presentation,
        # not another semantic decision, so it does not consume the decision budget.
        counted = (
            state.evolve()
            if isinstance(directive, Completed)
            else self.engine.note_model_call(state)
        )
        self.store.commit(
            counted,
            expected_revision=state.revision,
            events=(
                WorkEvent(
                    (
                        "model.terminal_response_request"
                        if isinstance(directive, Completed)
                        else "model.request"
                    ),
                    run_id,
                    getattr(getattr(directive, "context", None), "task_id", None),
                    {
                        "directive": type(directive).__name__,
                        "visible_tools": [getattr(item, "name", "") for item in visible],
                    },
                ),
            ),
        )
        updated_request = request.override(
            tools=visible,
            system_message=self._system_message(request.system_message, run_id, directive),
        )
        return updated_request, run_id

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        updated, run_id = self._prepare_model_request(request)
        response = handler(updated)
        self._record_model_response(run_id)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        updated, run_id = self._prepare_model_request(request)
        response = await handler(updated)
        self._record_model_response(run_id)
        return response

    def _runtime_tools(self, request: Any) -> tuple[Any, ...]:
        runtime = getattr(request, "runtime", None)
        tools = getattr(runtime, "tools", ())
        return tuple(tools or ())

    def _authorize_domain_call(self, request: Any) -> tuple[Any, ExecuteAction]:
        run_id = self._run_id(request.state)
        state = self.store.load(run_id)
        directive = self.engine.next(state, self._descriptors(self._runtime_tools(request)))
        if not isinstance(directive, NeedModelAction):
            if isinstance(directive, Failed) and state.status != RunStatus.FAILED:
                self._persist_failure(run_id, directive)
            raise ActionRejected(
                "WG.ACTION.NOT_EXPECTED",
                f"Current directive is {type(directive).__name__}",
                retryable=True,
            )
        name = str(request.tool_call["name"])
        candidate = next(
            (item for item in directive.context.actions if item.descriptor.name == name),
            None,
        )
        if candidate is None:
            raise ActionRejected("WG.ACTION.NOT_LEGAL", name, retryable=True)
        _, selected = self.engine.select_action(
            state,
            directive.context,
            ActionSelection(candidate.id, dict(request.tool_call.get("args", {}))),
        )
        if not isinstance(selected, ExecuteAction):
            raise ActionRejected("WG.ACTION.NOT_EXECUTABLE", name, retryable=True)
        call_id = request.tool_call.get("id")
        if call_id:
            operation_id = self._operation_id_for_call(state.id, str(call_id))
            intent = self.engine.intent_builder.build(
                run_id=selected.intent.run_id,
                task_id=selected.intent.task_id,
                candidate=candidate,
                selection=ActionSelection(candidate.id, dict(request.tool_call.get("args", {}))),
                attempt=selected.intent.attempt,
                operation_id=operation_id,
            )
            selected = ExecuteAction(intent, selected.requires_approval)
        return state, selected

    def _operation_id_for_call(self, run_id: str, call_id: str) -> str:
        """Return a replay-stable, known-failure-retryable operation attempt id.

        A successful/ambiguous tool call is replayed from its durable receipt. A
        receipt proving failure permits a new attempt with a new operation id. This
        composes safely with retry middleware that may re-enter WorkGraph using the
        same LangChain tool-call id.
        """
        prefix = f"{call_id}:"
        attempts = [
            item for item in self.store.operations(run_id) if item.operation_id.startswith(prefix)
        ]
        if not attempts:
            return f"{call_id}:1"
        attempts.sort(key=lambda item: item.operation_id)
        latest = attempts[-1]
        if not latest.result_json:
            return latest.operation_id
        receipt = self._receipt_from_json(latest.result_json)
        retryable = receipt.effect == EffectStatus.FAILURE or (
            receipt.effect == EffectStatus.UNKNOWN_EFFECT
            and latest.state == OperationState.RESOLVED
            and latest.resolution == "did_not_happen"
        )
        if not retryable:
            return latest.operation_id
        return f"{call_id}:{len(attempts) + 1}"

    @staticmethod
    def _receipt_json(receipt: ActionReceipt) -> str:
        return json.dumps(
            {
                "operation_id": receipt.operation_id,
                "run_id": receipt.run_id,
                "task_id": receipt.task_id,
                "tool_name": receipt.tool_name,
                "effect": receipt.effect.value,
                "output_summary": receipt.output_summary,
                "output_digest": receipt.output_digest,
                "failure_code": receipt.failure_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _receipt_from_json(raw: str) -> ActionReceipt:
        data = json.loads(raw)
        return ActionReceipt(
            operation_id=str(data["operation_id"]),
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            tool_name=str(data["tool_name"]),
            effect=EffectStatus(data["effect"]),
            output_summary=str(data.get("output_summary", "")),
            output_digest=data.get("output_digest"),
            failure_code=data.get("failure_code"),
        )

    def _apply_receipt(
        self,
        run_id: str,
        directive: ExecuteAction,
        receipt: ActionReceipt,
        *,
        execution_attempts: int,
    ) -> None:
        latest = self.store.load(run_id)
        updated = self.engine.record_receipt(
            latest,
            receipt,
            execution_attempts=execution_attempts,
        )
        if updated is latest:
            return
        self.store.commit(
            updated,
            expected_revision=latest.revision,
            events=(
                WorkEvent(
                    "action.result",
                    run_id,
                    directive.intent.task_id,
                    {
                        "tool_name": directive.intent.tool_name,
                        "effect": receipt.effect.value,
                        "failure_code": receipt.failure_code,
                    },
                    operation_id=directive.intent.operation_id,
                ),
            ),
        )

    def _claim(self, state: Any, directive: ExecuteAction) -> tuple[str, Any]:
        semantics = json.dumps(
            operation_semantics(directive.intent), sort_keys=True, separators=(",", ":")
        )
        claim = self.store.claim_operation(
            state.id,
            directive.intent.operation_id,
            semantics,
        )
        return semantics, claim

    def _cached_response(
        self, state: Any, directive: ExecuteAction, claim: Any, name: str, call_id: str
    ) -> Any:
        record = claim.record
        if record.result_json:
            receipt = self._receipt_from_json(record.result_json)
            self._apply_receipt(
                state.id,
                directive,
                receipt,
                execution_attempts=max(1, record.execution_count),
            )
            return ToolMessage(
                content=receipt.output_summary or receipt.effect.value,
                tool_call_id=call_id,
                name=name,
                status="error" if receipt.effect != EffectStatus.SUCCESS else "success",
            )
        receipt = ActionReceipt(
            directive.intent.operation_id,
            state.id,
            directive.intent.task_id,
            name,
            EffectStatus.UNKNOWN_EFFECT,
            output_summary="A previous execution crossed an ambiguous external-effect boundary.",
            failure_code="WG.ACTION.UNKNOWN_EFFECT",
        )
        self._apply_receipt(state.id, directive, receipt, execution_attempts=0)
        return ToolMessage(
            content="WG.ACTION.UNKNOWN_EFFECT: reconcile the prior operation before continuing.",
            tool_call_id=call_id,
            name=name,
            status="error",
        )

    def _finish_tool_call(
        self,
        state: Any,
        directive: ExecuteAction,
        semantics: str,
        name: str,
        call_id: str,
        *,
        response: Any | None = None,
        error: Exception | None = None,
    ) -> Any:
        if isinstance(error, UnknownEffectError):
            receipt = ActionReceipt(
                directive.intent.operation_id,
                state.id,
                directive.intent.task_id,
                name,
                EffectStatus.UNKNOWN_EFFECT,
                output_summary=error.message,
                failure_code=error.code,
            )
        elif error is not None:
            receipt = ActionReceipt(
                directive.intent.operation_id,
                state.id,
                directive.intent.task_id,
                name,
                EffectStatus.FAILURE,
                output_summary=str(error),
                failure_code=type(error).__name__,
            )
        else:
            content = str(getattr(response, "content", response))
            status = getattr(response, "status", "success")
            receipt = ActionReceipt(
                directive.intent.operation_id,
                state.id,
                directive.intent.task_id,
                name,
                EffectStatus.FAILURE if status == "error" else EffectStatus.SUCCESS,
                output_summary=content,
                failure_code="WG.ACTION.TOOL_ERROR" if status == "error" else None,
            )
        self.store.complete_operation(
            state.id,
            directive.intent.operation_id,
            semantics,
            self._receipt_json(receipt),
        )
        self._apply_receipt(state.id, directive, receipt, execution_attempts=1)
        if error is not None:
            return ToolMessage(
                content=f"{receipt.failure_code}: {receipt.output_summary}",
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        return response

    def _reconciliation_observation_allowed(self, request: Any, name: str) -> bool:
        run_id = self._run_id(request.state)
        state = self.store.load(run_id)
        if state.status != RunStatus.RECONCILING:
            return False
        return self.tool_policies.get(name, ToolPolicy()).read_only

    def _record_observation(self, request: Any, name: str) -> None:
        run_id = self._run_id(request.state)
        state = self.store.load(run_id)
        updated = state.evolve()
        self.store.commit(
            updated,
            expected_revision=state.revision,
            events=(WorkEvent("reconciliation.observation", run_id, data={"tool_name": name}),),
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        name = str(request.tool_call["name"])
        if name in CONTROL_TOOL_NAMES or name in self.native_tool_names:
            return handler(request)
        if self._reconciliation_observation_allowed(request, name):
            response = handler(request)
            self._record_observation(request, name)
            return response
        state, directive = self._authorize_domain_call(request)
        semantics, claim = self._claim(state, directive)
        call_id = str(request.tool_call.get("id") or directive.intent.operation_id)
        if not claim.acquired:
            return self._cached_response(state, directive, claim, name, call_id)
        try:
            response = handler(request)
        except Exception as exc:
            return self._finish_tool_call(state, directive, semantics, name, call_id, error=exc)
        return self._finish_tool_call(state, directive, semantics, name, call_id, response=response)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        name = str(request.tool_call["name"])
        if name in CONTROL_TOOL_NAMES or name in self.native_tool_names:
            return await handler(request)
        if self._reconciliation_observation_allowed(request, name):
            response = await handler(request)
            self._record_observation(request, name)
            return response
        state, directive = self._authorize_domain_call(request)
        semantics, claim = self._claim(state, directive)
        call_id = str(request.tool_call.get("id") or directive.intent.operation_id)
        if not claim.acquired:
            return self._cached_response(state, directive, claim, name, call_id)
        try:
            response = await handler(request)
        except Exception as exc:
            return self._finish_tool_call(state, directive, semantics, name, call_id, error=exc)
        return self._finish_tool_call(state, directive, semantics, name, call_id, response=response)

    @staticmethod
    def _next_task_id(state: Any, offset: int) -> str:
        maximum = max(
            (int(item[1:]) for item in state.tasks if item.startswith("T") and item[1:].isdigit()),
            default=0,
        )
        return f"T{maximum + offset}"

    def _build_control_tools(self) -> list[Any]:
        @tool
        def work_graph_status(runtime: ToolRuntime) -> str:
            """Return the complete authoritative WorkGraph projection for diagnostics."""
            state = self.store.load(self._run_id(runtime.state))
            return json.dumps(project(state), sort_keys=True)

        @tool
        def work_graph_expand(children: list[ExpansionChild], runtime: ToolRuntime) -> str:
            """Decompose a multi-stage objective into separately tracked children.

            Use this when stages must be independently completed or ordered;
            declare each child dependency by its local key. A parent cannot be
            submitted while any child remains open.
            """
            run_id = self._run_id(runtime.state)
            state = self.store.load(run_id)
            directive = self.engine.next(state, ())
            if not isinstance(directive, NeedModelAction):
                raise WorkGraphError("WG.EXPAND.NOT_READY", type(directive).__name__)
            key_to_id = {
                item.key: self._next_task_id(state, index)
                for index, item in enumerate(children, start=1)
            }
            specs = tuple(
                TaskSpec(
                    id=key_to_id[item.key],
                    title=item.title,
                    description=item.description,
                    dependencies=frozenset(key_to_id[key] for key in item.depends_on),
                    covers_requirements=frozenset(item.covers_requirements),
                    priority=item.priority,
                )
                for item in children
            )
            updated = self.engine.apply_expansion(
                state,
                ExpansionProposal(directive.context.task_id, specs),
            )
            self.store.commit(
                updated,
                expected_revision=state.revision,
                events=(
                    WorkEvent(
                        "graph.expanded",
                        run_id,
                        directive.context.task_id,
                        {"children": [item.id for item in specs]},
                    ),
                ),
            )
            return json.dumps(project(updated), sort_keys=True)

        @tool
        def work_graph_submit_result(
            summary: str,
            runtime: ToolRuntime,
            outputs: dict[str, Any] | None = None,
            facts: list[str] | None = None,
            artifacts: list[str] | None = None,
            evidence_refs: list[str] | None = None,
        ) -> str:
            """Submit the current semantic task result after its real work is complete."""
            run_id = self._run_id(runtime.state)
            state = self.store.load(run_id)
            directive = self.engine.next(state, ())
            if not isinstance(directive, NeedModelAction):
                raise WorkGraphError("WG.SUBMIT.NOT_READY", type(directive).__name__)
            result = TaskResult(
                summary=summary,
                outputs=dict(outputs or {}),
                facts=tuple(facts or ()),
                artifacts=tuple(artifacts or ()),
                evidence_refs=tuple(evidence_refs or ()),
            )
            updated = self.engine.submit_result(state, directive.context.task_id, result)
            self.store.commit(
                updated,
                expected_revision=state.revision,
                events=(WorkEvent("task.completed", run_id, directive.context.task_id),),
            )
            return json.dumps(project(updated), sort_keys=True)

        @tool
        def work_graph_retry(runtime: ToolRuntime) -> str:
            """Retry a safely retryable failed run after fixing or reconsidering the approach."""
            run_id = self._run_id(runtime.state)
            state = self.store.load(run_id)
            updated = self.engine.retry(state)
            self.store.commit(
                updated,
                expected_revision=state.revision,
                events=(WorkEvent("run.retried", run_id),),
            )
            return json.dumps(project(updated), sort_keys=True)

        @tool
        def work_graph_reconcile(
            operation_id: str,
            happened: bool,
            runtime: ToolRuntime,
        ) -> str:
            """Resolve an ambiguous external effect after determining whether it happened."""
            run_id = self._run_id(runtime.state)
            state = self.store.load(run_id)
            updated = self.engine.resolve_unknown_effect(
                state,
                operation_id,
                happened=happened,
            )
            self.store.resolve_operation(
                run_id,
                operation_id,
                resolution="happened" if happened else "did_not_happen",
            )
            self.store.commit(
                updated,
                expected_revision=state.revision,
                events=(
                    WorkEvent(
                        "action.reconciled",
                        run_id,
                        data={"happened": happened},
                        operation_id=operation_id,
                    ),
                ),
            )
            return json.dumps(project(updated), sort_keys=True)

        @tool
        def work_graph_repair(
            scope_task_id: str,
            reason: str,
            runtime: ToolRuntime,
            supersede_task_ids: list[str] | None = None,
            replacement_children: list[dict[str, Any]] | None = None,
        ) -> str:
            """Repair a broken region, preserving unrelated work.

            Supply replacement_children when the failed region needs a new
            executable work item; an empty patch only clears the failure and
            can leave the graph deadlocked.
            """
            run_id = self._run_id(runtime.state)
            state = self.store.load(run_id)
            replacement_children = replacement_children or []
            key_to_id = {
                key: self._next_task_id(state, index)
                for index, item in enumerate(replacement_children, start=1)
                for key in [str(item.get("key", item.get("id")))]
            }
            specs = tuple(
                TaskSpec(
                    id=key_to_id[str(item.get("key", item.get("id")))],
                    title=str(item["title"]),
                    description=str(item.get("description", "")),
                    dependencies=frozenset(
                        key_to_id.get(str(key), str(key))
                        for key in item.get("depends_on", item.get("dependencies", []))
                    ),
                    covers_requirements=frozenset(
                        str(value) for value in item.get("covers_requirements", [])
                    ),
                    priority=int(item.get("priority", 0)),
                )
                for item in replacement_children
            )
            patch = GraphPatch(
                scope_root=scope_task_id,
                add_tasks=specs,
                supersede_task_ids=frozenset(supersede_task_ids or ()),
                reason=reason,
            )
            updated = self.engine.apply_repair(state, patch)
            self.store.commit(
                updated,
                expected_revision=state.revision,
                events=(
                    WorkEvent(
                        "graph.repaired",
                        run_id,
                        scope_task_id,
                        {
                            "reason": reason,
                            "superseded": sorted(patch.supersede_task_ids),
                            "added": [item.id for item in specs],
                        },
                    ),
                ),
            )
            return json.dumps(project(updated), sort_keys=True)

        return [
            work_graph_status,
            work_graph_expand,
            work_graph_submit_result,
            work_graph_repair,
            work_graph_retry,
            work_graph_reconcile,
        ]

    def record_trusted_evidence(
        self,
        run_id: str,
        criterion_id: str,
        summary: str,
        *,
        source: str = "host",
    ) -> None:
        """Host-only API. There is intentionally no model tool for trusted evidence."""
        state = self.store.load(run_id)
        updated = self.engine.add_evidence(
            state,
            EvidenceRecord(criterion_id, source, True, summary),
        )
        self.store.commit(
            updated,
            expected_revision=state.revision,
            events=(
                WorkEvent(
                    "evidence.trusted",
                    run_id,
                    data={"criterion_id": criterion_id, "source": source},
                ),
            ),
        )


def create_work_graph_agent(*args: Any, **kwargs: Any) -> Any:
    """Create a Deep Agent with WorkGraph and automatic DeepAgents-native HITL policy."""
    if not integration_available():
        raise ImportError("Install work-graph-middleware[deepagents] to create an agent.")
    from deepagents import create_deep_agent

    middleware = list(kwargs.pop("middleware", ()))
    work_graph = kwargs.pop("work_graph_middleware", None) or WorkGraphMiddleware()
    middleware.insert(0, work_graph)

    interrupt_on = dict(kwargs.pop("interrupt_on", {}) or {})
    for name, policy in work_graph.tool_policies.items():
        if policy.requires_approval and name not in interrupt_on:
            interrupt_on[name] = {"allowed_decisions": ["approve", "edit", "reject"]}
    if interrupt_on:
        kwargs["interrupt_on"] = interrupt_on
    return create_deep_agent(*args, middleware=middleware, **kwargs)
