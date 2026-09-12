from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class WorkEvent:
    type: str
    run_id: str
    task_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int | None = None
    state_revision: int | None = None
    graph_revision: int | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    operation_id: str | None = None

    def stored(self, *, seq: int, state_revision: int, graph_revision: int) -> WorkEvent:
        return replace(
            self,
            seq=seq,
            state_revision=state_revision,
            graph_revision=graph_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "seq": self.seq,
            "type": self.type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "data": self.data,
            "timestamp": self.timestamp,
            "state_revision": self.state_revision,
            "graph_revision": self.graph_revision,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkEvent:
        return cls(
            type=str(value["type"]),
            run_id=str(value["run_id"]),
            task_id=value.get("task_id"),
            data=dict(value.get("data") or {}),
            timestamp=float(value.get("timestamp", time.time())),
            schema_version=int(value.get("schema_version", EVENT_SCHEMA_VERSION)),
            event_id=str(value.get("event_id") or uuid.uuid4().hex),
            seq=int(value["seq"]) if value.get("seq") is not None else None,
            state_revision=(
                int(value["state_revision"]) if value.get("state_revision") is not None else None
            ),
            graph_revision=(
                int(value["graph_revision"]) if value.get("graph_revision") is not None else None
            ),
            causation_id=value.get("causation_id"),
            correlation_id=value.get("correlation_id"),
            operation_id=value.get("operation_id"),
        )
