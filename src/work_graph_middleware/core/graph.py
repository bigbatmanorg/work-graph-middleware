from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from work_graph_middleware.core.models import RunState, RunStatus, Task, TaskStatus, WorkGraphConfig
from work_graph_middleware.errors import GraphValidationError


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    title: str
    description: str = ""
    dependencies: frozenset[str] = frozenset()
    covers_requirements: frozenset[str] = frozenset()
    priority: int = 0


@dataclass(frozen=True, slots=True)
class ExpansionProposal:
    parent_id: str
    children: tuple[TaskSpec, ...]


@dataclass(frozen=True, slots=True)
class GraphPatch:
    scope_root: str
    add_tasks: tuple[TaskSpec, ...] = ()
    supersede_task_ids: frozenset[str] = frozenset()
    reason: str = "repair"


def _children(state: RunState, parent_id: str) -> list[Task]:
    return [task for task in state.tasks.values() if task.parent_id == parent_id]


def task_depth(state: RunState, task_id: str) -> int:
    depth = 0
    task = state.tasks[task_id]
    seen: set[str] = set()
    while task.parent_id is not None:
        if task.id in seen:
            raise GraphValidationError("WG.GRAPH.PARENT_CYCLE", "Parent hierarchy contains a cycle")
        seen.add(task.id)
        depth += 1
        task = state.tasks[task.parent_id]
    return depth


def descendants(state: RunState, task_id: str) -> set[str]:
    result: set[str] = set()
    frontier = [task_id]
    while frontier:
        current = frontier.pop()
        direct = [
            task.id
            for task in state.tasks.values()
            if current in task.dependencies or task.parent_id == current
        ]
        for child in direct:
            if child not in result and child != task_id:
                result.add(child)
                frontier.append(child)
    return result


def validate_graph(state: RunState, config: WorkGraphConfig) -> None:
    if state.root_task_id not in state.tasks:
        raise GraphValidationError("WG.GRAPH.NO_ROOT", "Root task is missing")
    if len(state.tasks) > config.max_nodes:
        raise GraphValidationError("WG.GRAPH.MAX_NODES", "Graph node limit exceeded")
    for task in state.tasks.values():
        if task.parent_id is not None and task.parent_id not in state.tasks:
            raise GraphValidationError(
                "WG.GRAPH.UNKNOWN_PARENT", f"Unknown parent {task.parent_id}"
            )
        if task.id in task.dependencies:
            raise GraphValidationError(
                "WG.GRAPH.SELF_DEPENDENCY", f"Task {task.id} depends on itself"
            )
        unknown = task.dependencies - state.tasks.keys()
        if unknown:
            raise GraphValidationError(
                "WG.GRAPH.UNKNOWN_DEPENDENCY", f"Unknown dependencies: {sorted(unknown)}"
            )
        if task_depth(state, task.id) > config.max_depth:
            raise GraphValidationError("WG.GRAPH.MAX_DEPTH", f"Task {task.id} exceeds max depth")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise GraphValidationError("WG.GRAPH.CYCLE", "Dependency graph contains a cycle")
        visiting.add(task_id)
        for dependency in state.tasks[task_id].dependencies:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in state.tasks:
        visit(task_id)


def expected_input_fingerprint(state: RunState, task: Task) -> str:
    dependencies = []
    for dependency_id in sorted(task.dependencies):
        dependency = state.tasks[dependency_id]
        dependencies.append((dependency_id, dependency.output_fingerprint))
    payload = {
        "task": {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "requirements": sorted(task.covers_requirements),
        },
        "dependencies": dependencies,
        "goal_revision": state.goal.revision,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _clone_with_tasks(
    state: RunState, tasks: dict[str, Task], *, graph_bump: bool = True
) -> RunState:
    return state.evolve(tasks=tasks, graph_revision=state.graph_revision + (1 if graph_bump else 0))


def apply_expansion(
    state: RunState, proposal: ExpansionProposal, config: WorkGraphConfig
) -> RunState:
    if proposal.parent_id not in state.tasks:
        raise GraphValidationError("WG.GRAPH.UNKNOWN_PARENT", proposal.parent_id)
    parent = state.tasks[proposal.parent_id]
    existing_children = _children(state, proposal.parent_id)
    if existing_children:
        raise GraphValidationError("WG.GRAPH.ALREADY_EXPANDED", proposal.parent_id)
    if not proposal.children:
        raise GraphValidationError(
            "WG.GRAPH.EMPTY_EXPANSION",
            "Expansion must add at least one child",
        )

    tasks = dict(state.tasks)
    order = max((task.created_order for task in tasks.values()), default=0)
    ids = {spec.id for spec in proposal.children}
    if len(ids) != len(proposal.children):
        raise GraphValidationError("WG.GRAPH.DUPLICATE_NODE", "Expansion contains duplicate IDs")
    collision = ids & tasks.keys()
    if collision:
        raise GraphValidationError(
            "WG.GRAPH.DUPLICATE_NODE", f"Task IDs already exist: {sorted(collision)}"
        )
    allowed_dependencies = set(tasks) | ids
    for spec in proposal.children:
        unknown = set(spec.dependencies) - allowed_dependencies
        if unknown:
            raise GraphValidationError(
                "WG.GRAPH.UNKNOWN_DEPENDENCY", f"Unknown dependencies: {sorted(unknown)}"
            )
        order += 1
        tasks[spec.id] = Task(
            id=spec.id,
            title=spec.title,
            description=spec.description,
            parent_id=proposal.parent_id,
            dependencies=spec.dependencies,
            covers_requirements=spec.covers_requirements,
            priority=spec.priority,
            created_order=order,
        )
    tasks[parent.id] = parent.with_status(TaskStatus.WAITING)
    candidate = _clone_with_tasks(state, tasks)
    validate_graph(candidate, config)
    return candidate


def apply_patch(state: RunState, patch: GraphPatch, config: WorkGraphConfig) -> RunState:
    """Apply a repair limited to one graph region.

    Completed nodes are immutable. A failed/waiting scope root may be reopened when
    replacement children are added, which keeps repair local rather than rebuilding
    the entire graph.
    """

    if patch.scope_root not in state.tasks:
        raise GraphValidationError("WG.GRAPH.UNKNOWN_SCOPE", patch.scope_root)
    allowed = descendants(state, patch.scope_root) | {patch.scope_root}
    if not patch.supersede_task_ids <= allowed:
        raise GraphValidationError(
            "WG.GRAPH.PATCH_SCOPE", "Patch attempts to supersede outside its scope"
        )
    tasks = dict(state.tasks)
    for task_id in patch.supersede_task_ids:
        task = tasks[task_id]
        if task.status == TaskStatus.COMPLETED:
            raise GraphValidationError("WG.GRAPH.COMPLETED_IMMUTABLE", task_id)
        tasks[task_id] = task.with_status(TaskStatus.SUPERSEDED)

    order = max((task.created_order for task in tasks.values()), default=0)
    for spec in patch.add_tasks:
        if spec.id in tasks:
            raise GraphValidationError("WG.GRAPH.DUPLICATE_NODE", spec.id)
        order += 1
        tasks[spec.id] = Task(
            id=spec.id,
            title=spec.title,
            description=spec.description,
            parent_id=patch.scope_root,
            dependencies=spec.dependencies,
            covers_requirements=spec.covers_requirements,
            priority=spec.priority,
            created_order=order,
        )

    scope = tasks[patch.scope_root]
    if patch.add_tasks and scope.status in {TaskStatus.FAILED, TaskStatus.READY, TaskStatus.STALE}:
        tasks[patch.scope_root] = scope.with_status(
            TaskStatus.WAITING,
            last_failure_code=None,
            failure_count=0,
        )

    candidate = state.evolve(
        tasks=tasks,
        graph_revision=state.graph_revision + 1,
        status=RunStatus.ACTIVE,
        failure_code=None,
    )
    validate_graph(candidate, config)
    return candidate


def invalidate_descendants(state: RunState, changed_task_id: str) -> RunState:
    tasks = dict(state.tasks)
    changed = False
    for task_id in descendants(state, changed_task_id):
        task = tasks[task_id]
        if task.status == TaskStatus.COMPLETED:
            tasks[task_id] = task.with_status(
                TaskStatus.STALE,
                stale_reason=f"dependency {changed_task_id} changed",
            )
            changed = True
    if not changed:
        return state
    return _clone_with_tasks(state, tasks)


def requirements_covered(state: RunState) -> set[str]:
    covered: set[str] = set()
    for task in state.tasks.values():
        if task.status == TaskStatus.COMPLETED:
            covered.update(task.covers_requirements)
    return covered


def active_children(state: RunState, parent_id: str) -> Iterable[Task]:
    return (
        task
        for task in _children(state, parent_id)
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}
    )
