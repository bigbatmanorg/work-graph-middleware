from __future__ import annotations

from work_graph_middleware.actions.models import ActionDescriptor
from work_graph_middleware.core.engine import NeedModelAction, WorkGraphEngine
from work_graph_middleware.core.graph import (
    ExpansionProposal,
    TaskSpec,
    descendants,
    validate_graph,
)
from work_graph_middleware.core.models import GoalSpec, TaskResult, TaskStatus, WorkGraphConfig
from work_graph_middleware.presentation.projection import model_context, project


def test_descendants_include_parent_and_dependency_regions() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    state = engine.apply_expansion(
        state,
        ExpansionProposal(
            "T1",
            (
                TaskSpec("T2", "a"),
                TaskSpec("T3", "b", dependencies=frozenset({"T2"})),
            ),
        ),
    )
    assert descendants(state, "T2") == {"T3"}


def test_changed_completed_dependency_invalidates_descendant() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    state = engine.apply_expansion(
        state,
        ExpansionProposal(
            "T1",
            (
                TaskSpec("T2", "source"),
                TaskSpec("T3", "dependent", dependencies=frozenset({"T2"})),
            ),
        ),
    )
    state = engine.submit_result(state, "T2", TaskResult("v1"))
    state = engine.submit_result(state, "T3", TaskResult("derived"))
    # Re-submit an upstream task with changed output to simulate host-authorized refresh.
    from dataclasses import replace

    tasks = dict(state.tasks)
    tasks["T2"] = replace(tasks["T2"], status=TaskStatus.READY)
    state = state.evolve(tasks=tasks)
    state = engine.submit_result(state, "T2", TaskResult("v2"))
    assert state.tasks["T3"].status == TaskStatus.STALE


def test_ui_projection_contains_full_tasks_and_counters() -> None:
    state = WorkGraphEngine().create_run(GoalSpec("root"))
    data = project(state)
    assert data["run_id"] == state.id
    assert data["tasks"][0]["id"] == "T1"
    assert "effects_applied" in data["counters"]


def test_model_context_is_compact_not_full_projection() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("write report"))
    directive = engine.next(state, [ActionDescriptor("write", "write report")])
    assert isinstance(directive, NeedModelAction)
    data = model_context(state, directive)
    assert "tasks" not in data
    assert data["current_task"]["id"] == "T1"
    assert "write" in data["current_task"]["legal_actions"]


def test_graph_validate_accepts_normal_dag() -> None:
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    state = engine.apply_expansion(
        state,
        ExpansionProposal(
            "T1",
            (TaskSpec("T2", "a"), TaskSpec("T3", "b", dependencies=frozenset({"T2"}))),
        ),
    )
    validate_graph(state, WorkGraphConfig())


def test_graph_rejects_missing_root_and_max_nodes() -> None:
    from dataclasses import replace

    import pytest

    from work_graph_middleware.errors import GraphValidationError

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.NO_ROOT"):
        validate_graph(replace(state, root_task_id="missing"), WorkGraphConfig())
    with pytest.raises(GraphValidationError, match="WG.GRAPH.MAX_NODES"):
        validate_graph(state, WorkGraphConfig(max_nodes=0))


def test_graph_rejects_unknown_parent_dependency_and_self_dependency() -> None:
    from dataclasses import replace

    import pytest

    from work_graph_middleware.core.models import Task
    from work_graph_middleware.errors import GraphValidationError

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    bad_parent = replace(
        state,
        tasks={**state.tasks, "T2": Task("T2", "bad", parent_id="missing")},
    )
    with pytest.raises(GraphValidationError, match="WG.GRAPH.UNKNOWN_PARENT"):
        validate_graph(bad_parent, WorkGraphConfig())
    bad_dep = replace(
        state,
        tasks={**state.tasks, "T2": Task("T2", "bad", dependencies=frozenset({"missing"}))},
    )
    with pytest.raises(GraphValidationError, match="WG.GRAPH.UNKNOWN_DEPENDENCY"):
        validate_graph(bad_dep, WorkGraphConfig())
    bad_self = replace(
        state,
        tasks={**state.tasks, "T2": Task("T2", "bad", dependencies=frozenset({"T2"}))},
    )
    with pytest.raises(GraphValidationError, match="WG.GRAPH.SELF_DEPENDENCY"):
        validate_graph(bad_self, WorkGraphConfig())


def test_graph_rejects_dependency_cycle() -> None:
    from dataclasses import replace

    import pytest

    from work_graph_middleware.core.models import Task
    from work_graph_middleware.errors import GraphValidationError

    state = WorkGraphEngine().create_run(GoalSpec("root"))
    state = replace(
        state,
        tasks={
            "T1": state.tasks["T1"],
            "T2": Task("T2", "a", dependencies=frozenset({"T3"})),
            "T3": Task("T3", "b", dependencies=frozenset({"T2"})),
        },
    )
    with pytest.raises(GraphValidationError, match="WG.GRAPH.CYCLE"):
        validate_graph(state, WorkGraphConfig())


def test_graph_rejects_max_depth() -> None:
    import pytest

    from work_graph_middleware.errors import GraphValidationError

    engine = WorkGraphEngine(WorkGraphConfig(max_depth=10))
    state = engine.create_run(GoalSpec("root"))
    state = engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "child"),)))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.MAX_DEPTH"):
        validate_graph(state, WorkGraphConfig(max_depth=0))


def test_expansion_rejects_unknown_parent_empty_duplicate_and_existing_children() -> None:
    import pytest

    from work_graph_middleware.errors import GraphValidationError

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.UNKNOWN_PARENT"):
        engine.apply_expansion(state, ExpansionProposal("missing", (TaskSpec("T2", "x"),)))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.EMPTY_EXPANSION"):
        engine.apply_expansion(state, ExpansionProposal("T1", ()))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.DUPLICATE_NODE"):
        engine.apply_expansion(
            state,
            ExpansionProposal("T1", (TaskSpec("T2", "a"), TaskSpec("T2", "b"))),
        )
    with pytest.raises(GraphValidationError, match="WG.GRAPH.DUPLICATE_NODE"):
        engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T1", "collision"),)))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.UNKNOWN_DEPENDENCY"):
        engine.apply_expansion(
            state,
            ExpansionProposal("T1", (TaskSpec("T2", "x", dependencies=frozenset({"missing"})),)),
        )
    expanded = engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "x"),)))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.ALREADY_EXPANDED"):
        engine.apply_expansion(expanded, ExpansionProposal("T1", (TaskSpec("T3", "y"),)))


def test_patch_rejects_unknown_scope_out_of_scope_and_duplicate() -> None:
    import pytest

    from work_graph_middleware.core.graph import GraphPatch
    from work_graph_middleware.errors import GraphValidationError

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("root"))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.UNKNOWN_SCOPE"):
        engine.apply_repair(state, GraphPatch("missing"))
    state = engine.apply_expansion(state, ExpansionProposal("T1", (TaskSpec("T2", "child"),)))
    # Add an unrelated root-like node directly only for validating patch scope rules.
    from dataclasses import replace

    from work_graph_middleware.core.models import Task

    state = replace(state, tasks={**state.tasks, "T9": Task("T9", "unrelated")})
    with pytest.raises(GraphValidationError, match="WG.GRAPH.PATCH_SCOPE"):
        engine.apply_repair(state, GraphPatch("T2", supersede_task_ids=frozenset({"T9"})))
    with pytest.raises(GraphValidationError, match="WG.GRAPH.DUPLICATE_NODE"):
        engine.apply_repair(state, GraphPatch("T1", add_tasks=(TaskSpec("T2", "duplicate"),)))


def test_model_context_unknown_effect_branch() -> None:
    from work_graph_middleware.actions.models import ActionReceipt, ActionSelection
    from work_graph_middleware.core.models import EffectStatus

    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec("remote"))
    directive = engine.next(state, [ActionDescriptor("remote", "remote")])
    assert isinstance(directive, NeedModelAction)
    candidate = next(x for x in directive.context.actions if x.descriptor.name == "remote")
    _, selected = engine.select_action(state, directive.context, ActionSelection(candidate.id, {}))
    state = engine.record_receipt(
        state,
        ActionReceipt(
            selected.intent.operation_id,  # type: ignore[union-attr]
            state.id,
            "T1",
            "remote",
            EffectStatus.UNKNOWN_EFFECT,
        ),
    )
    directive = engine.next(state, [])
    data = model_context(state, directive)
    assert data["unknown_effects"][0]["tool_name"] == "remote"
