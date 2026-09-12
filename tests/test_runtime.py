from __future__ import annotations

from pathlib import Path

from work_graph_middleware.actions.models import ActionDescriptor, ActionSelection
from work_graph_middleware.core.engine import ExecuteAction, NeedModelAction, WorkGraphEngine
from work_graph_middleware.core.models import GoalSpec, TaskResult, WorkGraphConfig
from work_graph_middleware.errors import UnknownEffectError
from work_graph_middleware.persistence import MemoryStore, SQLiteStore
from work_graph_middleware.runtime import WorkGraphRuntime


def selected(runtime: WorkGraphRuntime, state, descriptor: ActionDescriptor) -> ExecuteAction:
    directive = runtime.engine.next(state, [descriptor])
    assert isinstance(directive, NeedModelAction)
    candidate = next(x for x in directive.context.actions if x.descriptor.name == descriptor.name)
    _, effect = runtime.engine.select_action(
        state,
        directive.context,
        ActionSelection(candidate.id, {"value": "x"}),
    )
    assert isinstance(effect, ExecuteAction)
    return effect


def test_runtime_executes_effect_once_on_replay() -> None:
    store = MemoryStore()
    runtime = WorkGraphRuntime(store)
    state = runtime.start(GoalSpec("write"))
    descriptor = ActionDescriptor("write", "write")
    directive = selected(runtime, state, descriptor)
    calls = {"count": 0}

    def write(value: str) -> str:
        calls["count"] += 1
        return value

    first = runtime.execute(state, directive, {"write": write})
    second = runtime.execute(first, directive, {"write": write})
    assert calls["count"] == 1
    assert second.counters.tool_calls == 1
    assert second.tasks["T1"].successful_host_actions == 1


def test_runtime_reserved_without_receipt_becomes_unknown_effect() -> None:
    store = MemoryStore()
    runtime = WorkGraphRuntime(store)
    state = runtime.start(GoalSpec("write"))
    descriptor = ActionDescriptor("write", "write")
    directive = selected(runtime, state, descriptor)
    import json

    from work_graph_middleware.actions.execution import operation_semantics

    semantics = json.dumps(
        operation_semantics(directive.intent),
        sort_keys=True,
        separators=(",", ":"),
    )
    store.claim_operation(state.id, directive.intent.operation_id, semantics)
    updated = runtime.execute(state, directive, {"write": lambda value: value})
    assert updated.unknown_effects
    assert updated.counters.tool_calls == 0


def test_runtime_unknown_tool_exception_requires_reconciliation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "runtime.db")
    runtime = WorkGraphRuntime(store)
    state = runtime.start(GoalSpec("remote"))
    descriptor = ActionDescriptor("remote", "remote")
    directive = selected(runtime, state, descriptor)

    def remote(value: str) -> str:
        raise UnknownEffectError("response lost")

    updated = runtime.execute(state, directive, {"remote": remote})
    assert updated.unknown_effects[0].operation_id == directive.intent.operation_id
    reconciled = runtime.reconcile(updated.id, directive.intent.operation_id, happened=True)
    assert not reconciled.unknown_effects
    assert reconciled.counters.effects_applied == 1


def test_runtime_known_failure_is_not_counted_as_effect() -> None:
    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("fail"))
    descriptor = ActionDescriptor("fail", "fail")
    directive = selected(runtime, state, descriptor)

    def fail(value: str) -> str:
        raise RuntimeError("known failure")

    updated = runtime.execute(state, directive, {"fail": fail})
    assert updated.counters.tool_calls == 1
    assert updated.counters.effects_applied == 0
    assert updated.tasks["T1"].failure_count == 1


def test_runtime_requires_approval_and_tool_presence() -> None:
    import pytest

    from work_graph_middleware.errors import ActionRejected

    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("x"))
    descriptor = ActionDescriptor("danger", "danger", requires_approval=True)
    directive = selected(runtime, state, descriptor)
    with pytest.raises(ActionRejected, match="WG.APPROVAL.REQUIRED"):
        runtime.execute(state, directive, {"danger": lambda value: value})

    descriptor = ActionDescriptor("missing", "missing")
    directive = selected(runtime, state, descriptor)
    with pytest.raises(ActionRejected, match="WG.ACTION.TOOL_UNAVAILABLE"):
        runtime.execute(state, directive, {})


def test_runtime_execute_approved_contract() -> None:
    import pytest

    from work_graph_middleware.actions.approval import ApprovalChoice, ApprovalDecision
    from work_graph_middleware.errors import ActionRejected

    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("x"))
    normal = selected(runtime, state, ActionDescriptor("write", "write"))
    with pytest.raises(ActionRejected, match="WG.APPROVAL.NOT_REQUIRED"):
        runtime.execute_approved(
            state,
            normal,
            ApprovalDecision(ApprovalChoice.APPROVE),
            {"write": lambda value: value},
        )

    approval_descriptor = ActionDescriptor("write", "write", requires_approval=True)
    approval_directive = selected(runtime, state, approval_descriptor)
    with pytest.raises(ActionRejected, match="WG.APPROVAL.REAUTHORIZE"):
        runtime.execute_approved(
            state,
            approval_directive,
            ApprovalDecision(ApprovalChoice.REJECT),
            {"write": lambda value: value},
        )
    done = runtime.execute_approved(
        state,
        approval_directive,
        ApprovalDecision(ApprovalChoice.APPROVE),
        {"write": lambda value: value},
    )
    assert done.tasks["T1"].successful_host_actions == 1


def test_runtime_step_executes_model_selection_and_records_event() -> None:
    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("write"))
    descriptor = ActionDescriptor("write", "write")

    def decide(directive: NeedModelAction) -> ActionSelection:
        candidate = next(x for x in directive.context.actions if x.descriptor.name == "write")
        return ActionSelection(candidate.id, {"value": "ok"})

    result = runtime.step(state.id, [descriptor], decide, {"write": lambda value: value})
    assert result.state.counters.model_calls == 1
    assert result.state.counters.tool_calls == 1
    assert any(event.type == "model.request" for event in result.events)


def test_runtime_step_invalid_decision_is_counted() -> None:
    import pytest

    from work_graph_middleware.errors import ActionRejected

    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("write"))
    with pytest.raises(ActionRejected):
        runtime.step(
            state.id,
            [ActionDescriptor("write", "write")],
            lambda _directive: ActionSelection("missing", {}),
            {"write": lambda: "x"},
        )
    latest = runtime.store.load(state.id)
    assert latest.counters.invalid_decisions == 1


def test_runtime_step_persists_failed_directive_and_terminal_noop() -> None:
    engine = WorkGraphEngine(WorkGraphConfig(max_model_calls=0))
    runtime = WorkGraphRuntime(MemoryStore(), engine)
    state = runtime.start(GoalSpec("x"))
    result = runtime.step(state.id, [], lambda _x: ActionSelection(""), {})
    assert result.state.status.value == "failed"
    again = runtime.step(state.id, [], lambda _x: ActionSelection(""), {})
    assert again.state.status.value == "failed"


def test_runtime_submit_repair_retry_helpers() -> None:
    from dataclasses import replace

    from work_graph_middleware.core.graph import GraphPatch, TaskSpec
    from work_graph_middleware.core.models import RunStatus, TaskStatus

    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("x"))
    submitted = runtime.submit_result(state.id, "T1", TaskResult("done"))
    assert submitted.status.value == "completed"

    # Separate run for repair/retry helpers.
    state2 = runtime.start(GoalSpec("repair"))
    failed = replace(
        state2,
        status=RunStatus.FAILED,
        failure_code="WG.TEST",
        tasks={"T1": replace(state2.tasks["T1"], status=TaskStatus.FAILED)},
    )
    runtime.store.commit(failed, expected_revision=state2.revision)
    repaired = runtime.repair(
        failed.id,
        GraphPatch("T1", add_tasks=(TaskSpec("T2", "fallback"),)),
    )
    assert repaired.status.value == "active"

    state3 = runtime.start(GoalSpec("retry"))
    failed3 = replace(
        state3,
        status=RunStatus.FAILED,
        failure_code="WG.TEST",
        tasks={"T1": replace(state3.tasks["T1"], status=TaskStatus.FAILED)},
    )
    runtime.store.commit(failed3, expected_revision=state3.revision)
    retried = runtime.retry(failed3.id)
    assert retried.status.value == "active"


def test_runtime_astap_need_model_action_and_terminal() -> None:
    import asyncio

    runtime = WorkGraphRuntime(MemoryStore())
    state = runtime.start(GoalSpec("x"))
    descriptor = ActionDescriptor("write", "write")

    async def decide(directive: NeedModelAction) -> ActionSelection:
        candidate = next(x for x in directive.context.actions if x.descriptor.name == "write")
        return ActionSelection(candidate.id, {"value": "ok"})

    result = asyncio.run(
        runtime.astep(state.id, [descriptor], decide, {"write": lambda value: value})
    )
    assert result.state.counters.tool_calls == 1
    completed = runtime.submit_result(result.state.id, "T1", TaskResult("done"))
    terminal = asyncio.run(runtime.astep(completed.id, [], decide, {}))
    assert terminal.state.status.value == "completed"
