from __future__ import annotations

from work_graph_middleware.core.graph import active_children, task_depth
from work_graph_middleware.core.models import RunState, Task, TaskStatus


class Scheduler:
    """Deterministic dependency scheduler for semantic tasks."""

    def ready_tasks(self, state: RunState) -> list[Task]:
        ready: list[Task] = []
        for task in state.tasks.values():
            children = [child for child in state.tasks.values() if child.parent_id == task.id]
            resumable_parent = (
                task.status == TaskStatus.WAITING
                and bool(children)
                and all(
                    child.status in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}
                    for child in children
                )
            )
            selectable = task.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.STALE}
            if not selectable and not resumable_parent:
                continue
            if any(True for _ in active_children(state, task.id)):
                continue
            dependencies = [state.tasks[dependency_id] for dependency_id in task.dependencies]
            if not all(dependency.status == TaskStatus.COMPLETED for dependency in dependencies):
                continue
            ready.append(task)
        return sorted(
            ready,
            key=lambda task: (
                -task.priority,
                task_depth(state, task.id),
                task.created_order,
                task.id,
            ),
        )
