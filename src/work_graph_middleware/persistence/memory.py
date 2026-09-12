from __future__ import annotations

import threading

from work_graph_middleware.core.models import RunState
from work_graph_middleware.errors import RevisionConflict, WorkGraphError
from work_graph_middleware.persistence.base import (
    OperationClaim,
    OperationRecord,
    OperationState,
)
from work_graph_middleware.presentation.events import WorkEvent


class MemoryStore:
    def __init__(self) -> None:
        self._states: dict[str, RunState] = {}
        self._events: dict[str, list[WorkEvent]] = {}
        self._operations: dict[tuple[str, str], OperationRecord] = {}
        self._lock = threading.RLock()

    def _stored_events(self, state: RunState, events: tuple[WorkEvent, ...]) -> list[WorkEvent]:
        existing = self._events.get(state.id, [])
        next_seq = (existing[-1].seq or 0) + 1 if existing else 1
        return [
            event.stored(
                seq=next_seq + index,
                state_revision=state.revision,
                graph_revision=state.graph_revision,
            )
            for index, event in enumerate(events)
        ]

    def create(self, state: RunState, events: tuple[WorkEvent, ...] = ()) -> None:
        with self._lock:
            if state.id in self._states:
                raise WorkGraphError("WG.STORE.ALREADY_EXISTS", state.id)
            self._states[state.id] = state
            self._events[state.id] = []
            self._events[state.id].extend(self._stored_events(state, events))

    def load(self, run_id: str) -> RunState:
        with self._lock:
            try:
                return self._states[run_id]
            except KeyError as exc:
                raise WorkGraphError("WG.STORE.NOT_FOUND", run_id) from exc

    def commit(
        self, state: RunState, *, expected_revision: int, events: tuple[WorkEvent, ...] = ()
    ) -> None:
        with self._lock:
            current = self.load(state.id)
            if current.revision != expected_revision:
                raise RevisionConflict()
            self._states[state.id] = state
            self._events[state.id].extend(self._stored_events(state, events))

    def events(self, run_id: str, *, after_seq: int = 0) -> tuple[WorkEvent, ...]:
        with self._lock:
            self.load(run_id)
            return tuple(item for item in self._events[run_id] if (item.seq or 0) > after_seq)

    def run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._states))

    def claim_operation(
        self, run_id: str, operation_id: str, semantics_digest: str
    ) -> OperationClaim:
        with self._lock:
            self.load(run_id)
            key = (run_id, operation_id)
            previous = self._operations.get(key)
            if previous is not None:
                if previous.semantics_digest != semantics_digest:
                    raise WorkGraphError("WG.ACTION.OPERATION_CONFLICT", operation_id)
                return OperationClaim(False, previous)
            record = OperationRecord.reserved(operation_id, semantics_digest)
            self._operations[key] = record
            return OperationClaim(True, record)

    def get_operation(self, run_id: str, operation_id: str) -> OperationRecord | None:
        with self._lock:
            return self._operations.get((run_id, operation_id))

    def complete_operation(
        self, run_id: str, operation_id: str, semantics_digest: str, result_json: str
    ) -> OperationRecord:
        with self._lock:
            key = (run_id, operation_id)
            previous = self._operations.get(key)
            if previous is None:
                raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
            if previous.semantics_digest != semantics_digest:
                raise WorkGraphError("WG.ACTION.OPERATION_CONFLICT", operation_id)
            if previous.result_json and previous.result_json != result_json:
                raise WorkGraphError("WG.ACTION.OPERATION_RESULT_CONFLICT", operation_id)
            record = OperationRecord(
                operation_id=operation_id,
                semantics_digest=semantics_digest,
                result_json=result_json,
                state=OperationState.COMPLETED,
                execution_count=max(1, previous.execution_count),
                reserved_at=previous.reserved_at,
                resolution=previous.resolution,
            )
            self._operations[key] = record
            return record

    def resolve_operation(
        self, run_id: str, operation_id: str, *, resolution: str
    ) -> OperationRecord:
        with self._lock:
            key = (run_id, operation_id)
            previous = self._operations.get(key)
            if previous is None:
                raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
            record = OperationRecord(
                operation_id=previous.operation_id,
                semantics_digest=previous.semantics_digest,
                result_json=previous.result_json,
                state=OperationState.RESOLVED,
                execution_count=previous.execution_count,
                reserved_at=previous.reserved_at,
                resolution=resolution,
            )
            self._operations[key] = record
            return record

    def operations(self, run_id: str) -> tuple[OperationRecord, ...]:
        with self._lock:
            self.load(run_id)
            return tuple(
                record
                for (candidate_run, _), record in sorted(self._operations.items())
                if candidate_run == run_id
            )
