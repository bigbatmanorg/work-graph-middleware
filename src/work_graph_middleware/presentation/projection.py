from __future__ import annotations

from typing import Any

from work_graph_middleware.core.engine import Directive, NeedModelAction
from work_graph_middleware.core.models import RunState


def project(state: RunState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "run_id": state.id,
        "status": state.status.value,
        "revision": state.revision,
        "graph_revision": state.graph_revision,
        "objective": state.goal.objective,
        "failure_code": state.failure_code,
        "counters": {
            "model_calls": state.counters.model_calls,
            "tool_calls": state.counters.tool_calls,
            "effects_applied": state.counters.effects_applied,
            "expansions": state.counters.expansions,
            "repairs": state.counters.repairs,
            "invalid_decisions": state.counters.invalid_decisions,
        },
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "parent_id": task.parent_id,
                "dependencies": sorted(task.dependencies),
                "covers_requirements": sorted(task.covers_requirements),
                "status": task.status.value,
                "priority": task.priority,
                "revision": task.revision,
                "successful_host_actions": task.successful_host_actions,
                "host_action_attempts": task.host_action_attempts,
                "failure_count": task.failure_count,
                "last_failure_code": task.last_failure_code,
                "stale_reason": task.stale_reason,
                "result": task.result.summary if task.result else None,
            }
            for task in sorted(state.tasks.values(), key=lambda item: item.created_order)
        ],
        "evidence": [
            {
                "criterion_id": item.criterion_id,
                "source": item.source,
                "trusted": item.trusted,
                "summary": item.summary,
            }
            for item in state.evidence
        ],
        "unknown_effects": [
            {
                "task_id": item.task_id,
                "operation_id": item.operation_id,
                "tool_name": item.tool_name,
                "failure_code": item.failure_code,
            }
            for item in state.unknown_effects
        ],
    }


def model_context(state: RunState, directive: Directive) -> dict[str, Any]:
    data: dict[str, Any] = {
        "run_id": state.id,
        "status": state.status.value,
        "objective": state.goal.objective,
        "directive": type(directive).__name__,
        "failure_code": state.failure_code,
    }
    if isinstance(directive, NeedModelAction):
        context = directive.context
        task = state.tasks[context.task_id]
        children = [item for item in state.tasks.values() if item.parent_id == task.id]
        data["current_task"] = {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "dependency_results": list(context.dependency_results),
            "known_facts": list(context.known_facts),
            "last_result": context.last_result,
            "children": [
                {"id": child.id, "title": child.title, "status": child.status.value}
                for child in sorted(children, key=lambda item: item.created_order)
            ],
            "legal_actions": [item.descriptor.name for item in context.actions],
        }
    elif state.unknown_effects:
        data["unknown_effects"] = [
            {
                "operation_id": item.operation_id,
                "task_id": item.task_id,
                "tool_name": item.tool_name,
            }
            for item in state.unknown_effects
        ]
    return data
