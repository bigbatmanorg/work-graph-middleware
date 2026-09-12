from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

from work_graph_middleware.actions.execution import IntentBuilder, candidate_by_id
from work_graph_middleware.actions.models import (
    ActionCandidate,
    ActionDescriptor,
    ActionIntent,
    ActionReceipt,
    ActionSelection,
    DecisionContext,
)
from work_graph_middleware.actions.policy import ActionPolicy, AllowAllPolicy
from work_graph_middleware.actions.resolver import ActionResolver, SimpleActionResolver
from work_graph_middleware.core.graph import (
    ExpansionProposal,
    GraphPatch,
    apply_expansion,
    apply_patch,
    expected_input_fingerprint,
    invalidate_descendants,
    requirements_covered,
    validate_graph,
)
from work_graph_middleware.core.models import (
    EffectStatus,
    EvidenceRecord,
    GoalSpec,
    PendingUnknownEffect,
    RunState,
    RunStatus,
    TaskResult,
    TaskStatus,
    WorkGraphConfig,
)
from work_graph_middleware.core.scheduler import Scheduler
from work_graph_middleware.errors import ActionRejected, WorkGraphError

EXPAND_ACTION = "WG_EXPAND"
SUBMIT_ACTION = "WG_SUBMIT"


@dataclass(frozen=True, slots=True)
class NeedModelAction:
    context: DecisionContext


@dataclass(frozen=True, slots=True)
class NeedExpansion:
    task_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExecuteAction:
    intent: ActionIntent
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class NeedReconciliation:
    effects: tuple[PendingUnknownEffect, ...]


@dataclass(frozen=True, slots=True)
class Completed:
    run_id: str


@dataclass(frozen=True, slots=True)
class Failed:
    code: str
    task_id: str | None = None


Directive = (
    NeedModelAction | NeedExpansion | ExecuteAction | NeedReconciliation | Completed | Failed
)


class WorkGraphEngine:
    def __init__(
        self,
        config: WorkGraphConfig | None = None,
        *,
        resolver: ActionResolver | None = None,
        policy: ActionPolicy | None = None,
    ) -> None:
        self.config = config or WorkGraphConfig()
        if self.config.max_concurrency != 1:
            raise WorkGraphError(
                "WG.CONFIG.CONCURRENCY_UNSUPPORTED",
                "WorkGraph v1 intentionally supports max_concurrency=1 only",
            )
        self.resolver = resolver or SimpleActionResolver()
        self.policy = policy or AllowAllPolicy()
        self.scheduler = Scheduler()
        self.intent_builder = IntentBuilder()

    def create_run(self, goal: GoalSpec) -> RunState:
        state = RunState.new(goal)
        validate_graph(state, self.config)
        return state

    def _budget_failure(self, state: RunState) -> str | None:
        elapsed = time.time() - state.created_at
        checks = (
            (state.counters.model_calls >= self.config.max_model_calls, "WG.BUDGET.MODEL_CALLS"),
            (state.counters.tool_calls >= self.config.max_tool_calls, "WG.BUDGET.TOOL_CALLS"),
            (state.counters.expansions >= self.config.max_expansions, "WG.BUDGET.EXPANSIONS"),
            (state.counters.repairs >= self.config.max_repairs, "WG.BUDGET.REPAIRS"),
            (
                state.counters.invalid_decisions >= self.config.max_invalid_decisions,
                "WG.BUDGET.INVALID_DECISIONS",
            ),
            (elapsed >= self.config.max_wall_time_seconds, "WG.BUDGET.WALL_TIME"),
        )
        return next((code for hit, code in checks if hit), None)

    def next(self, state: RunState, tools: Sequence[ActionDescriptor]) -> Directive:
        if state.status == RunStatus.COMPLETED:
            return Completed(state.id)
        if state.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
            return Failed(state.failure_code or "WG.RUN.TERMINAL")
        if state.unknown_effects:
            return NeedReconciliation(state.unknown_effects)
        if code := self._budget_failure(state):
            return Failed(code)

        ready = self.scheduler.ready_tasks(state)
        if not ready:
            if self._can_complete_run(state):
                return Completed(state.id)
            return Failed("WG.GRAPH.DEADLOCK")

        task = ready[0]
        candidates = tuple(
            candidate
            for candidate in self.resolver.resolve(
                task,
                tools,
                limit=self.config.max_actions,
                expose_all_below=self.config.expose_all_tools_below,
            )
            if self.policy.allow(state, task, candidate)
        )
        virtual = (
            ActionCandidate(
                id=EXPAND_ACTION,
                descriptor=ActionDescriptor(name="expand_task", description="Decompose this task"),
                score=1.0,
            ),
            ActionCandidate(
                id=SUBMIT_ACTION,
                descriptor=ActionDescriptor(name="submit_result", description="Submit task result"),
                score=0.9,
            ),
        )
        dependency_results = tuple(
            result.summary
            for dep in sorted(task.dependencies)
            if (result := state.tasks[dep].result) is not None
        )
        known_facts = tuple(
            fact
            for dep in sorted(task.dependencies)
            if (result := state.tasks[dep].result) is not None
            for fact in result.facts
        )
        return NeedModelAction(
            DecisionContext(
                run_id=state.id,
                graph_revision=state.graph_revision,
                task_id=task.id,
                objective=state.goal.objective,
                task_title=task.title,
                task_description=task.description,
                dependency_results=dependency_results,
                known_facts=known_facts,
                last_result=task.result.summary if task.result else None,
                actions=(*candidates, *virtual),
            )
        )

    def note_model_call(self, state: RunState) -> RunState:
        if state.counters.model_calls >= self.config.max_model_calls:
            raise WorkGraphError("WG.BUDGET.MODEL_CALLS", "Model call budget exhausted")
        return state.evolve(
            counters=replace(state.counters, model_calls=state.counters.model_calls + 1)
        )

    def note_invalid_decision(self, state: RunState) -> RunState:
        if state.counters.invalid_decisions >= self.config.max_invalid_decisions:
            raise WorkGraphError("WG.BUDGET.INVALID_DECISIONS", "Invalid-decision budget exhausted")
        return state.evolve(
            counters=replace(
                state.counters,
                invalid_decisions=state.counters.invalid_decisions + 1,
            )
        )

    def select_action(
        self,
        state: RunState,
        context: DecisionContext,
        selection: ActionSelection,
    ) -> tuple[RunState, Directive]:
        if context.run_id != state.id or context.graph_revision != state.graph_revision:
            raise ActionRejected(
                "WG.ACTION.STALE_CONTEXT", "Decision context is stale", retryable=True
            )
        task = state.tasks[context.task_id]
        if task.host_action_attempts >= self.config.max_task_attempts:
            raise ActionRejected(
                "WG.TASK.MAX_ATTEMPTS",
                f"Task {task.id} exhausted its action attempts",
            )
        if selection.action_id == EXPAND_ACTION:
            return state, NeedExpansion(task.id, "model requested decomposition")
        if selection.action_id == SUBMIT_ACTION:
            raise ActionRejected(
                "WG.ACTION.RESULT_REQUIRED",
                "Use submit_result() with a TaskResult",
                retryable=True,
            )
        candidate = candidate_by_id(context.actions, selection.action_id)
        intent = self.intent_builder.build(
            run_id=state.id,
            task_id=task.id,
            candidate=candidate,
            selection=selection,
            attempt=task.host_action_attempts + 1,
        )
        return state, ExecuteAction(intent, candidate.descriptor.requires_approval)

    def apply_expansion(self, state: RunState, proposal: ExpansionProposal) -> RunState:
        if state.counters.expansions >= self.config.max_expansions:
            raise WorkGraphError("WG.BUDGET.EXPANSIONS", "Expansion budget exhausted")
        updated = apply_expansion(state, proposal, self.config)
        return updated.evolve(
            counters=replace(updated.counters, expansions=state.counters.expansions + 1)
        )

    def apply_repair(self, state: RunState, patch: GraphPatch) -> RunState:
        if state.counters.repairs >= self.config.max_repairs:
            raise WorkGraphError("WG.BUDGET.REPAIRS", "Repair budget exhausted")
        updated = apply_patch(state, patch, self.config)
        return updated.evolve(
            counters=replace(updated.counters, repairs=state.counters.repairs + 1),
            status=RunStatus.ACTIVE,
            failure_code=None,
        )

    def record_receipt(
        self,
        state: RunState,
        receipt: ActionReceipt,
        *,
        execution_attempts: int = 1,
    ) -> RunState:
        if receipt.operation_id in state.applied_operations:
            return state
        if state.counters.tool_calls + execution_attempts > self.config.max_tool_calls:
            raise WorkGraphError("WG.BUDGET.TOOL_CALLS", "Tool call budget exhausted")
        if receipt.task_id not in state.tasks:
            raise ActionRejected("WG.ACTION.UNKNOWN_TASK", receipt.task_id)

        task = state.tasks[receipt.task_id]
        attempts = task.host_action_attempts + execution_attempts
        tasks = dict(state.tasks)
        unknown = state.unknown_effects
        counters = replace(
            state.counters,
            tool_calls=state.counters.tool_calls + execution_attempts,
        )
        run_status = RunStatus.ACTIVE
        failure_code = state.failure_code

        if receipt.effect == EffectStatus.SUCCESS:
            tasks[task.id] = replace(
                task,
                successful_host_actions=task.successful_host_actions + 1,
                host_action_attempts=attempts,
                failure_count=0,
                last_failure_code=None,
                status=TaskStatus.READY,
                revision=task.revision + 1,
            )
            counters = replace(counters, effects_applied=counters.effects_applied + 1)
        elif receipt.effect == EffectStatus.UNKNOWN_EFFECT:
            unknown = (
                *unknown,
                PendingUnknownEffect(
                    task.id,
                    receipt.operation_id,
                    receipt.tool_name,
                    receipt.failure_code or "WG.ACTION.UNKNOWN_EFFECT",
                ),
            )
            tasks[task.id] = replace(
                task,
                host_action_attempts=attempts,
                status=TaskStatus.WAITING,
                revision=task.revision + 1,
                last_failure_code=receipt.failure_code or "WG.ACTION.UNKNOWN_EFFECT",
            )
            run_status = RunStatus.RECONCILING
        else:
            failures = task.failure_count + 1
            exhausted = attempts >= self.config.max_task_attempts
            tasks[task.id] = replace(
                task,
                host_action_attempts=attempts,
                failure_count=failures,
                last_failure_code=receipt.failure_code or "WG.ACTION.FAILURE",
                status=TaskStatus.FAILED if exhausted else TaskStatus.READY,
                revision=task.revision + 1,
            )
            if exhausted:
                run_status = RunStatus.FAILED
                failure_code = "WG.TASK.MAX_ATTEMPTS"

        return state.evolve(
            tasks=tasks,
            unknown_effects=unknown,
            applied_operations=state.applied_operations | {receipt.operation_id},
            counters=counters,
            status=run_status,
            failure_code=failure_code,
        )

    def resolve_unknown_effect(
        self,
        state: RunState,
        operation_id: str,
        *,
        happened: bool,
    ) -> RunState:
        match = next(
            (item for item in state.unknown_effects if item.operation_id == operation_id), None
        )
        if match is None:
            raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
        task = state.tasks[match.task_id]
        tasks = dict(state.tasks)
        tasks[task.id] = replace(
            task,
            successful_host_actions=task.successful_host_actions + (1 if happened else 0),
            failure_count=0 if happened else task.failure_count,
            last_failure_code=None if happened else task.last_failure_code,
            status=TaskStatus.READY,
            revision=task.revision + 1,
        )
        remaining = tuple(
            item for item in state.unknown_effects if item.operation_id != operation_id
        )
        counters = state.counters
        if happened:
            counters = replace(counters, effects_applied=counters.effects_applied + 1)
        return state.evolve(
            tasks=tasks,
            unknown_effects=remaining,
            counters=counters,
            status=RunStatus.ACTIVE if not remaining else RunStatus.RECONCILING,
            failure_code=None if not remaining else state.failure_code,
        )

    def mark_failed(self, state: RunState, code: str, *, task_id: str | None = None) -> RunState:
        tasks = dict(state.tasks)
        if task_id is not None and task_id in tasks:
            task = tasks[task_id]
            tasks[task_id] = task.with_status(TaskStatus.FAILED, last_failure_code=code)
        return state.evolve(status=RunStatus.FAILED, failure_code=code, tasks=tasks)

    def submit_result(self, state: RunState, task_id: str, result: TaskResult) -> RunState:
        if task_id not in state.tasks:
            raise WorkGraphError("WG.TASK.NOT_FOUND", task_id)
        task = state.tasks[task_id]
        children = [child for child in state.tasks.values() if child.parent_id == task_id]
        if any(
            child.status not in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED} for child in children
        ):
            raise WorkGraphError(
                "WG.TASK.CHILDREN_OPEN", "Task still has incomplete children", retryable=True
            )
        if (
            self.config.require_host_action_before_external_completion
            and not children
            and task.successful_host_actions == 0
            and result.artifacts
        ):
            raise WorkGraphError(
                "WG.TASK.NO_HOST_ACTION",
                "Artifact-producing task needs a successful host action",
                retryable=True,
            )
        old_output = task.output_fingerprint
        completed = replace(
            task,
            status=TaskStatus.COMPLETED,
            result=result,
            input_fingerprint=expected_input_fingerprint(state, task),
            output_fingerprint=result.fingerprint(),
            stale_reason=None,
            failure_count=0,
            last_failure_code=None,
            revision=task.revision + 1,
        )
        tasks = dict(state.tasks)
        tasks[task_id] = completed
        updated = state.evolve(tasks=tasks)
        if old_output is not None and old_output != completed.output_fingerprint:
            updated = invalidate_descendants(updated, task_id)
        if self._can_complete_run(updated):
            updated = updated.evolve(status=RunStatus.COMPLETED, failure_code=None)
        return updated

    def add_evidence(self, state: RunState, evidence: EvidenceRecord) -> RunState:
        updated = state.evolve(evidence=(*state.evidence, evidence))
        if self._can_complete_run(updated):
            updated = updated.evolve(status=RunStatus.COMPLETED, failure_code=None)
        return updated

    def cancel(self, state: RunState) -> RunState:
        if state.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise WorkGraphError("WG.RUN.TERMINAL", "Terminal runs cannot be cancelled")
        return state.evolve(status=RunStatus.CANCELLED, failure_code="WG.RUN.CANCELLED")

    def retry(self, state: RunState) -> RunState:
        if state.status != RunStatus.FAILED or state.unknown_effects:
            raise WorkGraphError("WG.RUN.NOT_RETRYABLE", "Run is not safely retryable")
        tasks = {
            task_id: (
                replace(
                    task,
                    status=TaskStatus.READY,
                    host_action_attempts=0,
                    failure_count=0,
                    last_failure_code=None,
                    revision=task.revision + 1,
                )
                if task.status == TaskStatus.FAILED
                else task
            )
            for task_id, task in state.tasks.items()
        }
        return state.evolve(status=RunStatus.ACTIVE, failure_code=None, tasks=tasks)

    def _can_complete_run(self, state: RunState) -> bool:
        root = state.tasks[state.root_task_id]
        if root.status != TaskStatus.COMPLETED or state.unknown_effects:
            return False
        required = {req.id for req in state.goal.requirements}
        if not required <= requirements_covered(state):
            return False
        evidence_by_criterion = {item.criterion_id: item for item in state.evidence}
        for criterion in state.goal.acceptance_criteria:
            evidence = evidence_by_criterion.get(criterion.id)
            if evidence is None:
                return False
            if criterion.requires_trusted_evidence and not evidence.trusted:
                return False
        return True
