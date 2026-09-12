from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from work_graph_middleware.core.models import EffectStatus


@dataclass(frozen=True, slots=True)
class ActionDescriptor:
    name: str
    description: str
    args_schema: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    requires_approval: bool = False
    fixed_arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    id: str
    descriptor: ActionDescriptor
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class DecisionContext:
    run_id: str
    graph_revision: int
    task_id: str
    objective: str
    task_title: str
    task_description: str
    dependency_results: tuple[str, ...]
    known_facts: tuple[str, ...]
    last_result: str | None
    actions: tuple[ActionCandidate, ...]


@dataclass(frozen=True, slots=True)
class ActionSelection:
    action_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionIntent:
    operation_id: str
    run_id: str
    task_id: str
    action_id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_digest: str
    attempt: int


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    operation_id: str
    run_id: str
    task_id: str
    tool_name: str
    effect: EffectStatus
    output_summary: str = ""
    output_digest: str | None = None
    failure_code: str | None = None
