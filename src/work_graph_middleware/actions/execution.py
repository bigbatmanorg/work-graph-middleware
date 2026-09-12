from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from work_graph_middleware.actions.models import ActionCandidate, ActionIntent, ActionSelection
from work_graph_middleware.errors import ActionRejected


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(slots=True)
class IntentBuilder:
    def build(
        self,
        *,
        run_id: str,
        task_id: str,
        candidate: ActionCandidate,
        selection: ActionSelection,
        attempt: int,
        operation_id: str | None = None,
    ) -> ActionIntent:
        arguments = dict(selection.arguments)
        for key, fixed_value in candidate.descriptor.fixed_arguments.items():
            if key in arguments and arguments[key] != fixed_value:
                raise ActionRejected(
                    "WG.ACTION.FIXED_ARGUMENT_CONFLICT",
                    f"Argument {key!r} is host-fixed",
                )
            arguments[key] = fixed_value
        return ActionIntent(
            operation_id=operation_id or uuid.uuid4().hex,
            run_id=run_id,
            task_id=task_id,
            action_id=candidate.id,
            tool_name=candidate.descriptor.name,
            arguments=arguments,
            arguments_digest=_digest(arguments),
            attempt=attempt,
        )


def candidate_by_id(candidates: tuple[ActionCandidate, ...], action_id: str) -> ActionCandidate:
    for candidate in candidates:
        if candidate.id == action_id:
            return candidate
    raise ActionRejected(
        "WG.ACTION.NOT_LEGAL", f"Unknown or stale action {action_id}", retryable=True
    )


def operation_semantics(intent: ActionIntent) -> Mapping[str, Any]:
    return {
        "run_id": intent.run_id,
        "task_id": intent.task_id,
        "tool_name": intent.tool_name,
        "arguments_digest": intent.arguments_digest,
    }
