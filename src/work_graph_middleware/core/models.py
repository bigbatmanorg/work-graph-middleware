from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

STATE_SCHEMA_VERSION = 1


class RunStatus(StrEnum):
    ACTIVE = "active"
    WAITING_HUMAN = "waiting_human"
    WAITING_APPROVAL = "waiting_approval"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    EXPANDING = "expanding"
    WAITING = "waiting"
    COMPLETED = "completed"
    STALE = "stale"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class EffectStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    text: str
    requires_trusted_evidence: bool = True


@dataclass(frozen=True, slots=True)
class GoalSpec:
    objective: str
    requirements: tuple[Requirement, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    revision: int = 1


@dataclass(frozen=True, slots=True)
class TaskResult:
    summary: str
    outputs: dict[str, Any] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        payload = {
            "summary": self.summary,
            "outputs": self.outputs,
            "facts": self.facts,
            "artifacts": self.artifacts,
            "evidence_refs": self.evidence_refs,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    description: str = ""
    parent_id: str | None = None
    dependencies: frozenset[str] = frozenset()
    covers_requirements: frozenset[str] = frozenset()
    priority: int = 0
    status: TaskStatus = TaskStatus.PENDING
    created_order: int = 0
    revision: int = 1
    input_fingerprint: str | None = None
    output_fingerprint: str | None = None
    result: TaskResult | None = None
    successful_host_actions: int = 0
    host_action_attempts: int = 0
    failure_count: int = 0
    last_failure_code: str | None = None
    stale_reason: str | None = None

    def with_status(self, status: TaskStatus, **changes: Any) -> Task:
        return replace(self, status=status, revision=self.revision + 1, **changes)


@dataclass(frozen=True, slots=True)
class BudgetCounters:
    model_calls: int = 0
    tool_calls: int = 0
    effects_applied: int = 0
    expansions: int = 0
    repairs: int = 0
    invalid_decisions: int = 0


@dataclass(frozen=True, slots=True)
class WorkGraphConfig:
    max_actions: int = 6
    expose_all_tools_below: int = 12
    max_depth: int = 6
    max_nodes: int = 100
    max_model_calls: int = 100
    max_tool_calls: int = 200
    max_expansions: int = 50
    max_repairs: int = 20
    max_invalid_decisions: int = 12
    max_task_attempts: int = 4
    max_wall_time_seconds: float = 3600.0
    max_concurrency: int = 1
    require_host_action_before_external_completion: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    criterion_id: str
    source: str
    trusted: bool
    summary: str


@dataclass(frozen=True, slots=True)
class PendingUnknownEffect:
    task_id: str
    operation_id: str
    tool_name: str
    failure_code: str = "WG.ACTION.UNKNOWN_EFFECT"


@dataclass(frozen=True, slots=True)
class RunState:
    id: str
    goal: GoalSpec
    root_task_id: str
    tasks: dict[str, Task]
    status: RunStatus = RunStatus.ACTIVE
    revision: int = 0
    graph_revision: int = 1
    counters: BudgetCounters = BudgetCounters()
    evidence: tuple[EvidenceRecord, ...] = ()
    unknown_effects: tuple[PendingUnknownEffect, ...] = ()
    applied_operations: frozenset[str] = frozenset()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    failure_code: str | None = None
    schema_version: int = STATE_SCHEMA_VERSION

    @classmethod
    def new(cls, goal: GoalSpec) -> RunState:
        run_id = uuid.uuid4().hex
        root_id = "T1"
        root = Task(id=root_id, title=goal.objective, created_order=1)
        return cls(id=run_id, goal=goal, root_task_id=root_id, tasks={root_id: root})

    def evolve(self, **changes: Any) -> RunState:
        return replace(self, revision=self.revision + 1, updated_at=time.time(), **changes)
