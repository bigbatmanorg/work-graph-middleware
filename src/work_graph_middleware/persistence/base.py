from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from work_graph_middleware.core.models import RunState
from work_graph_middleware.presentation.events import WorkEvent


class OperationState(StrEnum):
    RESERVED = "reserved"
    COMPLETED = "completed"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    semantics_digest: str
    result_json: str = ""
    state: OperationState = OperationState.RESERVED
    execution_count: int = 0
    reserved_at: float = 0.0
    resolution: str | None = None

    @classmethod
    def reserved(cls, operation_id: str, semantics_digest: str) -> OperationRecord:
        return cls(operation_id, semantics_digest, reserved_at=time.time())


@dataclass(frozen=True, slots=True)
class OperationClaim:
    acquired: bool
    record: OperationRecord


class WorkGraphStore(Protocol):
    def create(self, state: RunState, events: tuple[WorkEvent, ...] = ()) -> None: ...
    def load(self, run_id: str) -> RunState: ...
    def commit(
        self, state: RunState, *, expected_revision: int, events: tuple[WorkEvent, ...] = ()
    ) -> None: ...
    def events(self, run_id: str, *, after_seq: int = 0) -> tuple[WorkEvent, ...]: ...
    def run_ids(self) -> tuple[str, ...]: ...
    def claim_operation(
        self, run_id: str, operation_id: str, semantics_digest: str
    ) -> OperationClaim: ...
    def get_operation(self, run_id: str, operation_id: str) -> OperationRecord | None: ...
    def complete_operation(
        self, run_id: str, operation_id: str, semantics_digest: str, result_json: str
    ) -> OperationRecord: ...
    def resolve_operation(
        self, run_id: str, operation_id: str, *, resolution: str
    ) -> OperationRecord: ...
    def operations(self, run_id: str) -> tuple[OperationRecord, ...]: ...
