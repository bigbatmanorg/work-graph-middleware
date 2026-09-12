from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from work_graph_middleware.actions.models import ActionIntent
from work_graph_middleware.errors import ActionRejected


class ApprovalChoice(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    operation_id: str
    intent_digest: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    choice: ApprovalChoice
    edited_arguments: dict[str, Any] | None = None


def intent_digest(intent: ActionIntent) -> str:
    payload = {
        "operation_id": intent.operation_id,
        "run_id": intent.run_id,
        "task_id": intent.task_id,
        "tool_name": intent.tool_name,
        "arguments_digest": intent.arguments_digest,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def approval_request(intent: ActionIntent) -> ApprovalRequest:
    return ApprovalRequest(
        operation_id=intent.operation_id,
        intent_digest=intent_digest(intent),
        tool_name=intent.tool_name,
        arguments=dict(intent.arguments),
    )


def validate_approval(
    request: ApprovalRequest, intent: ActionIntent, decision: ApprovalDecision
) -> None:
    if request.operation_id != intent.operation_id or request.intent_digest != intent_digest(
        intent
    ):
        raise ActionRejected("WG.APPROVAL.STALE", "Approval does not match the exact action intent")
    if decision.choice == ApprovalChoice.EDIT and decision.edited_arguments is None:
        raise ActionRejected("WG.APPROVAL.INVALID_EDIT", "Edited approval requires arguments")
