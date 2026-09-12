from __future__ import annotations

import json
from typing import Any

from work_graph_middleware.core.models import (
    AcceptanceCriterion,
    BudgetCounters,
    EvidenceRecord,
    GoalSpec,
    PendingUnknownEffect,
    Requirement,
    RunState,
    RunStatus,
    Task,
    TaskResult,
    TaskStatus,
)


def _result_to_dict(value: TaskResult | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "summary": value.summary,
        "outputs": value.outputs,
        "facts": list(value.facts),
        "artifacts": list(value.artifacts),
        "evidence_refs": list(value.evidence_refs),
    }


def state_to_dict(state: RunState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "id": state.id,
        "goal": {
            "objective": state.goal.objective,
            "requirements": [{"id": x.id, "text": x.text} for x in state.goal.requirements],
            "acceptance_criteria": [
                {
                    "id": x.id,
                    "text": x.text,
                    "requires_trusted_evidence": x.requires_trusted_evidence,
                }
                for x in state.goal.acceptance_criteria
            ],
            "revision": state.goal.revision,
        },
        "root_task_id": state.root_task_id,
        "tasks": {
            task_id: {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "parent_id": task.parent_id,
                "dependencies": sorted(task.dependencies),
                "covers_requirements": sorted(task.covers_requirements),
                "priority": task.priority,
                "status": task.status.value,
                "created_order": task.created_order,
                "revision": task.revision,
                "input_fingerprint": task.input_fingerprint,
                "output_fingerprint": task.output_fingerprint,
                "result": _result_to_dict(task.result),
                "successful_host_actions": task.successful_host_actions,
                "host_action_attempts": task.host_action_attempts,
                "failure_count": task.failure_count,
                "last_failure_code": task.last_failure_code,
                "stale_reason": task.stale_reason,
            }
            for task_id, task in state.tasks.items()
        },
        "status": state.status.value,
        "revision": state.revision,
        "graph_revision": state.graph_revision,
        "counters": {
            "model_calls": state.counters.model_calls,
            "tool_calls": state.counters.tool_calls,
            "effects_applied": state.counters.effects_applied,
            "expansions": state.counters.expansions,
            "repairs": state.counters.repairs,
            "invalid_decisions": state.counters.invalid_decisions,
        },
        "evidence": [
            {
                "criterion_id": x.criterion_id,
                "source": x.source,
                "trusted": x.trusted,
                "summary": x.summary,
            }
            for x in state.evidence
        ],
        "unknown_effects": [
            {
                "task_id": x.task_id,
                "operation_id": x.operation_id,
                "tool_name": x.tool_name,
                "failure_code": x.failure_code,
            }
            for x in state.unknown_effects
        ],
        "applied_operations": sorted(state.applied_operations),
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "failure_code": state.failure_code,
    }


def state_from_dict(value: dict[str, Any]) -> RunState:
    goal_data = value["goal"]
    goal = GoalSpec(
        objective=str(goal_data["objective"]),
        requirements=tuple(
            Requirement(str(item["id"]), str(item["text"]))
            for item in goal_data.get("requirements", [])
        ),
        acceptance_criteria=tuple(
            AcceptanceCriterion(
                str(item["id"]),
                str(item["text"]),
                bool(item.get("requires_trusted_evidence", True)),
            )
            for item in goal_data.get("acceptance_criteria", [])
        ),
        revision=int(goal_data.get("revision", 1)),
    )
    tasks: dict[str, Task] = {}
    for task_id, item in value["tasks"].items():
        result_data = item.get("result")
        result = None
        if result_data is not None:
            result = TaskResult(
                summary=str(result_data["summary"]),
                outputs=dict(result_data.get("outputs") or {}),
                facts=tuple(str(x) for x in result_data.get("facts", [])),
                artifacts=tuple(str(x) for x in result_data.get("artifacts", [])),
                evidence_refs=tuple(str(x) for x in result_data.get("evidence_refs", [])),
            )
        tasks[str(task_id)] = Task(
            id=str(item["id"]),
            title=str(item["title"]),
            description=str(item.get("description", "")),
            parent_id=item.get("parent_id"),
            dependencies=frozenset(str(x) for x in item.get("dependencies", [])),
            covers_requirements=frozenset(str(x) for x in item.get("covers_requirements", [])),
            priority=int(item.get("priority", 0)),
            status=TaskStatus(item.get("status", "pending")),
            created_order=int(item.get("created_order", 0)),
            revision=int(item.get("revision", 1)),
            input_fingerprint=item.get("input_fingerprint"),
            output_fingerprint=item.get("output_fingerprint"),
            result=result,
            successful_host_actions=int(item.get("successful_host_actions", 0)),
            host_action_attempts=int(item.get("host_action_attempts", 0)),
            failure_count=int(item.get("failure_count", 0)),
            last_failure_code=item.get("last_failure_code"),
            stale_reason=item.get("stale_reason"),
        )
    counters_data = value.get("counters", {})
    return RunState(
        id=str(value["id"]),
        goal=goal,
        root_task_id=str(value["root_task_id"]),
        tasks=tasks,
        status=RunStatus(value.get("status", "active")),
        revision=int(value.get("revision", 0)),
        graph_revision=int(value.get("graph_revision", 1)),
        counters=BudgetCounters(
            model_calls=int(counters_data.get("model_calls", 0)),
            tool_calls=int(counters_data.get("tool_calls", 0)),
            effects_applied=int(counters_data.get("effects_applied", 0)),
            expansions=int(counters_data.get("expansions", 0)),
            repairs=int(counters_data.get("repairs", 0)),
            invalid_decisions=int(counters_data.get("invalid_decisions", 0)),
        ),
        evidence=tuple(
            EvidenceRecord(
                str(item["criterion_id"]),
                str(item["source"]),
                bool(item["trusted"]),
                str(item["summary"]),
            )
            for item in value.get("evidence", [])
        ),
        unknown_effects=tuple(
            PendingUnknownEffect(
                str(item["task_id"]),
                str(item["operation_id"]),
                str(item["tool_name"]),
                str(item.get("failure_code", "WG.ACTION.UNKNOWN_EFFECT")),
            )
            for item in value.get("unknown_effects", [])
        ),
        applied_operations=frozenset(str(x) for x in value.get("applied_operations", [])),
        created_at=float(value.get("created_at", 0.0)),
        updated_at=float(value.get("updated_at", 0.0)),
        failure_code=value.get("failure_code"),
        schema_version=int(value.get("schema_version", 1)),
    )


def state_to_json(state: RunState) -> str:
    return json.dumps(state_to_dict(state), sort_keys=True, separators=(",", ":"), default=str)


def state_from_json(raw: str) -> RunState:
    return state_from_dict(json.loads(raw))
