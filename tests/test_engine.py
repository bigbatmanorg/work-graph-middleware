from __future__ import annotations

from dataclasses import replace

import pytest

from work_graph_middleware.actions.models import ActionDescriptor, ActionReceipt, ActionSelection
from work_graph_middleware.core.engine import (
    ExecuteAction,
    Failed,
    NeedModelAction,
    WorkGraphEngine,
)
from work_graph_middleware.core.graph import ExpansionProposal, GraphPatch, TaskSpec
from work_graph_middleware.core.models import (
    AcceptanceCriterion,
    EffectStatus,
    EvidenceRecord,
    GoalSpec,
    RunStatus,
    TaskResult,
    TaskStatus,
    WorkGraphConfig,
)
from work_graph_middleware.errors import WorkGraphError


def tool(name: str = "write_file") -> ActionDescriptor:
    return ActionDescriptor(name=name, description=f"{name} content")


def select_first_domain(engine: WorkGraphEngine, state):
    directive = engine.next(state, [tool()])
    assert isinstance(directive, NeedModelAction)
    candidate = next(
        item for item in directive.context.actions if item.descriptor.name == "write_file"
    )
    _, selected = engine.select_action(
        state, directive.context, ActionSelection(candidate.id, {"path": "x"})
    )
    assert isinstance(selected, ExecuteAction)
    return selected


def receipt(selected: ExecuteAction, effect: EffectStatus, code: str | None = None):
    intent = selected.intent
    return ActionReceipt(
        intent.operation_id,
        intent.run_id,
        intent.task_id,
        intent.tool_name,
        effect,
        failure_code=code,
    )


def test_concurrency_other_than_one_is_explicitly_unsupported() -> None:
    with pytest.raises(WorkGraphError, match="WG.CONFIG.CONCURRENCY_UNSUPPORTED"):
        WorkGraphEngine(WorkGraphConfig(max_concurrency=2))


def test_small_tool_sets_are_all_visible() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_actions=2, expose_all_tools_below=5))
    state = engine.create_run(GoalSpec("use fourth tool"))
    directive = engine.next(
        state,
        [tool("alpha"), tool("beta"), tool("gamma"), tool("fourth")],
    )
    assert isinstance(directive, NeedModelAction)
    names = {item.descriptor.name for item in directive.context.actions}
    assert {"alpha", "beta", "gamma", "fourth"} <= names


def test_model_call_counter_is_explicit() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    updated = engine.note_model_call(state)
    assert updated.counters.model_calls == 1
    assert state.counters.model_calls == 0


def test_success_receipt_applied_exactly_once() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("write"))
    selected = select_first_domain(engine, state)
    item = receipt(selected, EffectStatus.SUCCESS)
    first = engine.record_receipt(state, item)
    second = engine.record_receipt(first, item)
    assert first.counters.tool_calls == 1
    assert first.counters.effects_applied == 1
    assert first.tasks["T1"].successful_host_actions == 1
    assert second == first


def test_failure_receipt_is_retryable_before_attempt_limit() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_task_attempts=2))
    state = engine.create_run(GoalSpec("write"))
    selected = select_first_domain(engine, state)
    failed = engine.record_receipt(state, receipt(selected, EffectStatus.FAILURE, "temporary"))
    assert failed.status == RunStatus.ACTIVE
    assert failed.tasks["T1"].status == TaskStatus.READY
    assert failed.tasks["T1"].failure_count == 1


def test_attempt_limit_persists_run_failure() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_task_attempts=2))
    state = engine.create_run(GoalSpec("write"))
    first = select_first_domain(engine, state)
    state = engine.record_receipt(state, receipt(first, EffectStatus.FAILURE, "one"))
    second = select_first_domain(engine, state)
    state = engine.record_receipt(state, receipt(second, EffectStatus.FAILURE, "two"))
    assert state.status == RunStatus.FAILED
    assert state.failure_code == "WG.TASK.MAX_ATTEMPTS"
    assert state.tasks["T1"].status == TaskStatus.FAILED


def test_retry_reopens_failed_task_and_resets_attempt_window() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_task_attempts=1))
    state = engine.create_run(GoalSpec("write"))
    selected = select_first_domain(engine, state)
    state = engine.record_receipt(state, receipt(selected, EffectStatus.FAILURE))
    retried = engine.retry(state)
    assert retried.status == RunStatus.ACTIVE
    assert retried.tasks["T1"].status == TaskStatus.READY
    assert retried.tasks["T1"].host_action_attempts == 0


def test_unknown_effect_blocks_normal_execution_until_reconciled() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("write"))
    selected = select_first_domain(engine, state)
    state = engine.record_receipt(
        state, receipt(selected, EffectStatus.UNKNOWN_EFFECT, "WG.ACTION.UNKNOWN_EFFECT")
    )
    directive = engine.next(state, [tool()])
    assert type(directive).__name__ == "NeedReconciliation"
    assert state.status == RunStatus.RECONCILING
    resolved = engine.resolve_unknown_effect(state, selected.intent.operation_id, happened=True)
    assert resolved.status == RunStatus.ACTIVE
    assert resolved.tasks["T1"].successful_host_actions == 1
    assert resolved.counters.effects_applied == 1


def test_reconcile_did_not_happen_does_not_count_success() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("write"))
    selected = select_first_domain(engine, state)
    state = engine.record_receipt(state, receipt(selected, EffectStatus.UNKNOWN_EFFECT))
    resolved = engine.resolve_unknown_effect(state, selected.intent.operation_id, happened=False)
    assert resolved.tasks["T1"].successful_host_actions == 0
    assert resolved.counters.effects_applied == 0


def test_expansion_resumes_parent_after_children_complete() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("bundle"))
    state = engine.apply_expansion(
        state,
        ExpansionProposal(
            "T1",
            (
                TaskSpec("T2", "first"),
                TaskSpec("T3", "second", dependencies=frozenset({"T2"})),
            ),
        ),
    )
    assert isinstance(engine.next(state, []), NeedModelAction)
    assert engine.next(state, []).context.task_id == "T2"  # type: ignore[union-attr]
    state = engine.submit_result(state, "T2", TaskResult("first done"))
    assert engine.next(state, []).context.task_id == "T3"  # type: ignore[union-attr]
    state = engine.submit_result(state, "T3", TaskResult("second done"))
    assert engine.next(state, []).context.task_id == "T1"  # type: ignore[union-attr]


def test_scoped_repair_cannot_supersede_completed_node() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("bundle"))
    state = engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "child"),)))
    state = engine.submit_result(state, "T2", TaskResult("done"))
    with pytest.raises(WorkGraphError, match="WG.GRAPH.COMPLETED_IMMUTABLE"):
        engine.apply_repair(
            state,
            GraphPatch(scope_root="T1", supersede_task_ids=frozenset({"T2"})),
        )


def test_local_repair_reopens_failed_scope_without_touching_other_work() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("bundle"))
    state = replace(
        state,
        status=RunStatus.FAILED,
        failure_code="x",
        tasks={"T1": replace(state.tasks["T1"], status=TaskStatus.FAILED)},
    )
    repaired = engine.apply_repair(
        state,
        GraphPatch("T1", add_tasks=(TaskSpec("T2", "fallback"),), reason="use fallback"),
    )
    assert repaired.status == RunStatus.ACTIVE
    assert repaired.tasks["T1"].status == TaskStatus.WAITING
    assert repaired.tasks["T2"].parent_id == "T1"


def test_artifact_leaf_requires_real_host_action() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("artifact"))
    with pytest.raises(WorkGraphError, match="WG.TASK.NO_HOST_ACTION"):
        engine.submit_result(state, "T1", TaskResult("done", artifacts=("x.txt",)))


def test_trusted_evidence_gate_blocks_then_allows_completion() -> None:
    engine = WorkGraphEngine()
    goal = GoalSpec(
        "answer",
        acceptance_criteria=(AcceptanceCriterion("c1", "host verified"),),
    )
    state = engine.create_run(goal)
    state = engine.submit_result(state, "T1", TaskResult("done"))
    assert state.status != RunStatus.COMPLETED
    state = engine.add_evidence(state, EvidenceRecord("c1", "model", False, "claim"))
    assert state.status != RunStatus.COMPLETED
    state = engine.add_evidence(state, EvidenceRecord("c1", "host", True, "verified"))
    assert state.status == RunStatus.COMPLETED


def test_budget_failure_is_directive_then_can_be_persisted() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_model_calls=1))
    state = engine.create_run(GoalSpec("x"))
    state = engine.note_model_call(state)
    directive = engine.next(state, [])
    assert isinstance(directive, Failed)
    assert directive.code == "WG.BUDGET.MODEL_CALLS"
    state = engine.mark_failed(state, directive.code)
    assert state.status == RunStatus.FAILED


def test_next_terminal_completed_and_cancelled() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    state = engine.submit_result(state, "T1", TaskResult("done"))
    assert type(engine.next(state, [])).__name__ == "Completed"
    cancelled = replace(state, status=RunStatus.CANCELLED, failure_code="cancelled")
    directive = engine.next(cancelled, [])
    assert isinstance(directive, Failed)
    assert directive.code == "cancelled"


def test_deadlock_directive_when_no_ready_and_not_complete() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    state = replace(state, tasks={"T1": replace(state.tasks["T1"], status=TaskStatus.WAITING)})
    directive = engine.next(state, [])
    assert isinstance(directive, Failed)
    assert directive.code == "WG.GRAPH.DEADLOCK"


def test_model_and_invalid_decision_budget_raises() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_model_calls=0, max_invalid_decisions=0))
    state = engine.create_run(GoalSpec("x"))
    with pytest.raises(WorkGraphError, match="WG.BUDGET.MODEL_CALLS"):
        engine.note_model_call(state)
    with pytest.raises(WorkGraphError, match="WG.BUDGET.INVALID_DECISIONS"):
        engine.note_invalid_decision(state)


def test_select_action_stale_context_and_virtual_actions() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    directive = engine.next(state, [tool()])
    assert isinstance(directive, NeedModelAction)
    stale = replace(directive.context, graph_revision=99)
    with pytest.raises(Exception, match="WG.ACTION.STALE_CONTEXT"):
        engine.select_action(state, stale, ActionSelection("A1"))
    _, expansion = engine.select_action(state, directive.context, ActionSelection("WG_EXPAND"))
    assert type(expansion).__name__ == "NeedExpansion"
    with pytest.raises(Exception, match="WG.ACTION.RESULT_REQUIRED"):
        engine.select_action(state, directive.context, ActionSelection("WG_SUBMIT"))


def test_select_action_attempt_limit_checked_even_before_receipt() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_task_attempts=1))
    state = engine.create_run(GoalSpec("x"))
    state = replace(state, tasks={"T1": replace(state.tasks["T1"], host_action_attempts=1)})
    directive = engine.next(state, [tool()])
    assert isinstance(directive, NeedModelAction)
    with pytest.raises(Exception, match="WG.TASK.MAX_ATTEMPTS"):
        engine.select_action(state, directive.context, ActionSelection("A1"))


def test_expansion_and_repair_budgets_raise() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_expansions=0, max_repairs=0))
    state = engine.create_run(GoalSpec("x"))
    with pytest.raises(WorkGraphError, match="WG.BUDGET.EXPANSIONS"):
        engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "x"),)))
    with pytest.raises(WorkGraphError, match="WG.BUDGET.REPAIRS"):
        engine.apply_repair(state, GraphPatch("T1"))


def test_tool_budget_and_unknown_task_receipts_raise() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_tool_calls=0))
    state = engine.create_run(GoalSpec("x"))
    fake = ActionReceipt("op", state.id, "T1", "x", EffectStatus.SUCCESS)
    with pytest.raises(WorkGraphError, match="WG.BUDGET.TOOL_CALLS"):
        engine.record_receipt(state, fake)
    engine = WorkGraphEngine()
    fake = ActionReceipt("op", state.id, "T404", "x", EffectStatus.SUCCESS)
    with pytest.raises(Exception, match="WG.ACTION.UNKNOWN_TASK"):
        engine.record_receipt(state, fake)


def test_resolve_unknown_missing_operation_raises() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    with pytest.raises(WorkGraphError, match="WG.ACTION.UNKNOWN_OPERATION"):
        engine.resolve_unknown_effect(state, "missing", happened=True)


def test_mark_failed_can_mark_specific_task() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    failed = engine.mark_failed(state, "WG.TEST", task_id="T1")
    assert failed.tasks["T1"].status == TaskStatus.FAILED
    assert failed.tasks["T1"].last_failure_code == "WG.TEST"


def test_submit_unknown_task_and_parent_with_open_children_raise() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    with pytest.raises(WorkGraphError, match="WG.TASK.NOT_FOUND"):
        engine.submit_result(state, "T404", TaskResult("x"))
    state = engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "child"),)))
    with pytest.raises(WorkGraphError, match="WG.TASK.CHILDREN_OPEN"):
        engine.submit_result(state, "T1", TaskResult("too early"))


def test_cancel_active_and_reject_terminal_cancel() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    cancelled = engine.cancel(state)
    assert cancelled.status == RunStatus.CANCELLED
    with pytest.raises(WorkGraphError, match="WG.RUN.TERMINAL"):
        engine.cancel(cancelled)


def test_retry_rejects_active_or_unknown_effect_run() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x"))
    with pytest.raises(WorkGraphError, match="WG.RUN.NOT_RETRYABLE"):
        engine.retry(state)
    selected = select_first_domain(engine, state)
    state = engine.record_receipt(state, receipt(selected, EffectStatus.UNKNOWN_EFFECT))
    state = replace(state, status=RunStatus.FAILED)
    with pytest.raises(WorkGraphError, match="WG.RUN.NOT_RETRYABLE"):
        engine.retry(state)


def test_requirement_coverage_is_required_for_completion() -> None:
    from work_graph_middleware.core.models import Requirement

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("x", requirements=(Requirement("r1", "must cover"),)))
    state = engine.submit_result(state, "T1", TaskResult("done"))
    assert state.status != RunStatus.COMPLETED
