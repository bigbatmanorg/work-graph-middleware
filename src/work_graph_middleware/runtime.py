"""Standalone deterministic runtime used by tests and advanced hosts.

Deep Agents is the supported agent integration. This runtime deliberately keeps
model conversation state outside the WorkGraph core and is useful for deterministic
certification, replay and embedding the engine in a custom host.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from work_graph_middleware.actions.approval import (
    ApprovalChoice,
    ApprovalDecision,
    approval_request,
    validate_approval,
)
from work_graph_middleware.actions.execution import operation_semantics
from work_graph_middleware.actions.models import ActionDescriptor, ActionReceipt, ActionSelection
from work_graph_middleware.core.engine import (
    ExecuteAction,
    Failed,
    NeedModelAction,
    WorkGraphEngine,
)
from work_graph_middleware.core.graph import GraphPatch
from work_graph_middleware.core.models import EffectStatus, GoalSpec, RunState, TaskResult
from work_graph_middleware.errors import ActionRejected, UnknownEffectError
from work_graph_middleware.persistence.base import WorkGraphStore
from work_graph_middleware.presentation.events import WorkEvent

Tool = Callable[..., Any]
DecisionMaker = Callable[[NeedModelAction], ActionSelection]
AsyncDecisionMaker = Callable[[NeedModelAction], Awaitable[ActionSelection]]


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    state: RunState
    events: tuple[WorkEvent, ...]


class WorkGraphRuntime:
    def __init__(self, store: WorkGraphStore, engine: WorkGraphEngine | None = None) -> None:
        self.store = store
        self.engine = engine or WorkGraphEngine()

    def start(self, goal: GoalSpec) -> RunState:
        state = self.engine.create_run(goal)
        self.store.create(state, (WorkEvent("run.created", state.id),))
        return state

    def _commit(self, previous: RunState, state: RunState, *events: WorkEvent) -> None:
        self.store.commit(state, expected_revision=previous.revision, events=tuple(events))

    @staticmethod
    def _receipt_from_json(raw: str) -> ActionReceipt:
        item = json.loads(raw)
        return ActionReceipt(
            operation_id=str(item["operation_id"]),
            run_id=str(item["run_id"]),
            task_id=str(item["task_id"]),
            tool_name=str(item["tool_name"]),
            effect=EffectStatus(item["effect"]),
            output_summary=str(item.get("output_summary", "")),
            output_digest=item.get("output_digest"),
            failure_code=item.get("failure_code"),
        )

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

    def _apply_receipt(
        self,
        state: RunState,
        directive: ExecuteAction,
        receipt: ActionReceipt,
        *,
        execution_attempts: int,
    ) -> RunState:
        updated = self.engine.record_receipt(
            state,
            receipt,
            execution_attempts=execution_attempts,
        )
        if updated is state:
            return state
        self._commit(
            state,
            updated,
            WorkEvent(
                "action.result",
                state.id,
                directive.intent.task_id,
                {
                    "tool_name": directive.intent.tool_name,
                    "effect": receipt.effect.value,
                    "failure_code": receipt.failure_code,
                },
                operation_id=directive.intent.operation_id,
            ),
        )
        return updated

    def execute(
        self, state: RunState, directive: ExecuteAction, tools: Mapping[str, Tool]
    ) -> RunState:
        intent = directive.intent
        if directive.requires_approval:
            raise ActionRejected(
                "WG.APPROVAL.REQUIRED",
                "Host approval is required before this action may execute",
                retryable=True,
            )
        semantics = json.dumps(operation_semantics(intent), sort_keys=True, separators=(",", ":"))
        claim = self.store.claim_operation(state.id, intent.operation_id, semantics)

        if claim.acquired:
            tool = tools.get(intent.tool_name)
            if tool is None:
                raise ActionRejected("WG.ACTION.TOOL_UNAVAILABLE", intent.tool_name, retryable=True)
            try:
                output = tool(**intent.arguments)
                receipt = ActionReceipt(
                    intent.operation_id,
                    state.id,
                    intent.task_id,
                    intent.tool_name,
                    EffectStatus.SUCCESS,
                    output_summary=str(output),
                )
            except UnknownEffectError as exc:
                receipt = ActionReceipt(
                    intent.operation_id,
                    state.id,
                    intent.task_id,
                    intent.tool_name,
                    EffectStatus.UNKNOWN_EFFECT,
                    output_summary=exc.message,
                    failure_code=exc.code,
                )
            except Exception as exc:  # known failure, safe for semantic retry
                receipt = ActionReceipt(
                    intent.operation_id,
                    state.id,
                    intent.task_id,
                    intent.tool_name,
                    EffectStatus.FAILURE,
                    output_summary=str(exc),
                    failure_code=type(exc).__name__,
                )
            self.store.complete_operation(
                state.id,
                intent.operation_id,
                semantics,
                self._receipt_json(receipt),
            )
            return self._apply_receipt(
                state,
                directive,
                receipt,
                execution_attempts=1,
            )

        existing = claim.record
        if existing.result_json:
            receipt = self._receipt_from_json(existing.result_json)
            return self._apply_receipt(
                state,
                directive,
                receipt,
                execution_attempts=max(1, existing.execution_count),
            )

        # Someone reserved this operation but there is no durable outcome. It may
        # have crossed the external boundary, so replay is forbidden until a host
        # or model-facing reconciliation decision resolves it.
        receipt = ActionReceipt(
            intent.operation_id,
            state.id,
            intent.task_id,
            intent.tool_name,
            EffectStatus.UNKNOWN_EFFECT,
            failure_code="WG.ACTION.UNKNOWN_EFFECT",
        )
        return self._apply_receipt(state, directive, receipt, execution_attempts=0)

    def execute_approved(
        self,
        state: RunState,
        directive: ExecuteAction,
        approval: ApprovalDecision,
        tools: Mapping[str, Tool],
    ) -> RunState:
        if not directive.requires_approval:
            raise ActionRejected("WG.APPROVAL.NOT_REQUIRED", "Action does not require approval")
        validate_approval(approval_request(directive.intent), directive.intent, approval)
        if approval.choice != ApprovalChoice.APPROVE:
            raise ActionRejected(
                "WG.APPROVAL.REAUTHORIZE",
                "Edited or rejected approval must re-enter normal authorization",
                retryable=True,
            )
        return self.execute(state, ExecuteAction(directive.intent, False), tools)

    def step(
        self,
        run_id: str,
        tools: Sequence[ActionDescriptor],
        decision: DecisionMaker,
        executors: Mapping[str, Tool],
    ) -> RuntimeResult:
        state = self.store.load(run_id)
        directive = self.engine.next(state, tools)
        if isinstance(directive, Failed) and state.failure_code != directive.code:
            failed = self.engine.mark_failed(state, directive.code, task_id=directive.task_id)
            self._commit(
                state,
                failed,
                WorkEvent("run.failed", run_id, data={"code": directive.code}),
            )
            state = failed
            return RuntimeResult(state, self.store.events(run_id))
        if not isinstance(directive, NeedModelAction):
            return RuntimeResult(state, self.store.events(run_id))

        counted = self.engine.note_model_call(state)
        self._commit(
            state,
            counted,
            WorkEvent("model.request", run_id, directive.context.task_id),
        )
        state = counted
        try:
            selected_state, effect = self.engine.select_action(
                state,
                directive.context,
                decision(directive),
            )
        except ActionRejected:
            invalid = self.engine.note_invalid_decision(state)
            self._commit(state, invalid, WorkEvent("model.invalid_decision", run_id))
            raise
        if isinstance(effect, ExecuteAction):
            state = self.execute(selected_state, effect, executors)
        return RuntimeResult(state, self.store.events(run_id))

    async def astep(
        self,
        run_id: str,
        tools: Sequence[ActionDescriptor],
        decision: AsyncDecisionMaker,
        executors: Mapping[str, Tool],
    ) -> RuntimeResult:
        state = self.store.load(run_id)
        directive = self.engine.next(state, tools)
        if not isinstance(directive, NeedModelAction):
            return await asyncio.to_thread(
                self.step,
                run_id,
                tools,
                lambda _x: ActionSelection(""),
                executors,
            )
        selection = await decision(directive)
        return await asyncio.to_thread(
            self.step,
            run_id,
            tools,
            lambda _directive: selection,
            executors,
        )

    def submit_result(self, run_id: str, task_id: str, result: TaskResult) -> RunState:
        state = self.store.load(run_id)
        updated = self.engine.submit_result(state, task_id, result)
        self._commit(state, updated, WorkEvent("task.completed", run_id, task_id))
        return updated

    def repair(self, run_id: str, patch: GraphPatch) -> RunState:
        state = self.store.load(run_id)
        updated = self.engine.apply_repair(state, patch)
        self._commit(
            state,
            updated,
            WorkEvent(
                "graph.repaired",
                run_id,
                patch.scope_root,
                {"reason": patch.reason, "superseded": sorted(patch.supersede_task_ids)},
            ),
        )
        return updated

    def retry(self, run_id: str) -> RunState:
        state = self.store.load(run_id)
        updated = self.engine.retry(state)
        self._commit(state, updated, WorkEvent("run.retried", run_id))
        return updated

    def reconcile(self, run_id: str, operation_id: str, *, happened: bool) -> RunState:
        state = self.store.load(run_id)
        updated = self.engine.resolve_unknown_effect(state, operation_id, happened=happened)
        self.store.resolve_operation(
            run_id,
            operation_id,
            resolution="happened" if happened else "did_not_happen",
        )
        self._commit(
            state,
            updated,
            WorkEvent(
                "action.reconciled",
                run_id,
                data={"happened": happened},
                operation_id=operation_id,
            ),
        )
        return updated
