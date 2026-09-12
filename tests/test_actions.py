from __future__ import annotations

from dataclasses import replace

import pytest

from work_graph_middleware.actions.approval import (
    ApprovalChoice,
    ApprovalDecision,
    approval_request,
    intent_digest,
    validate_approval,
)
from work_graph_middleware.actions.execution import (
    IntentBuilder,
    candidate_by_id,
    operation_semantics,
)
from work_graph_middleware.actions.models import (
    ActionCandidate,
    ActionDescriptor,
    ActionSelection,
)
from work_graph_middleware.actions.resolver import SimpleActionResolver
from work_graph_middleware.core.models import Task
from work_graph_middleware.errors import ActionRejected


def candidate(fixed=None):
    return ActionCandidate(
        "A1",
        ActionDescriptor("write", "write text", fixed_arguments=fixed or {}),
        1.0,
    )


def test_intent_builder_merges_host_fixed_arguments() -> None:
    intent = IntentBuilder().build(
        run_id="r",
        task_id="T1",
        candidate=candidate({"workspace": "/safe"}),
        selection=ActionSelection("A1", {"path": "x"}),
        attempt=2,
        operation_id="op",
    )
    assert intent.arguments == {"path": "x", "workspace": "/safe"}
    assert intent.operation_id == "op"
    assert intent.attempt == 2


def test_fixed_argument_conflict_is_rejected() -> None:
    with pytest.raises(ActionRejected, match="WG.ACTION.FIXED_ARGUMENT_CONFLICT"):
        IntentBuilder().build(
            run_id="r",
            task_id="T1",
            candidate=candidate({"workspace": "/safe"}),
            selection=ActionSelection("A1", {"workspace": "/evil"}),
            attempt=1,
        )


def test_candidate_lookup_and_unknown_action() -> None:
    item = candidate()
    assert candidate_by_id((item,), "A1") is item
    with pytest.raises(ActionRejected, match="WG.ACTION.NOT_LEGAL"):
        candidate_by_id((item,), "missing")


def test_operation_semantics_excludes_operation_id_but_binds_action() -> None:
    intent = IntentBuilder().build(
        run_id="r",
        task_id="T1",
        candidate=candidate(),
        selection=ActionSelection("A1", {"x": 1}),
        attempt=1,
        operation_id="op",
    )
    semantics = operation_semantics(intent)
    assert semantics["run_id"] == "r"
    assert semantics["task_id"] == "T1"
    assert semantics["tool_name"] == "write"
    assert "operation_id" not in semantics


def test_approval_request_binds_exact_intent() -> None:
    intent = IntentBuilder().build(
        run_id="r",
        task_id="T1",
        candidate=candidate(),
        selection=ActionSelection("A1", {"x": 1}),
        attempt=1,
        operation_id="op",
    )
    request = approval_request(intent)
    assert request.operation_id == "op"
    assert request.intent_digest == intent_digest(intent)
    validate_approval(request, intent, ApprovalDecision(ApprovalChoice.APPROVE))


def test_stale_approval_rejected() -> None:
    intent = IntentBuilder().build(
        run_id="r",
        task_id="T1",
        candidate=candidate(),
        selection=ActionSelection("A1", {"x": 1}),
        attempt=1,
        operation_id="op",
    )
    request = approval_request(intent)
    changed = replace(intent, arguments_digest="changed")
    with pytest.raises(ActionRejected, match="WG.APPROVAL.STALE"):
        validate_approval(request, changed, ApprovalDecision(ApprovalChoice.APPROVE))


def test_edit_requires_arguments() -> None:
    intent = IntentBuilder().build(
        run_id="r",
        task_id="T1",
        candidate=candidate(),
        selection=ActionSelection("A1", {}),
        attempt=1,
    )
    with pytest.raises(ActionRejected, match="WG.APPROVAL.INVALID_EDIT"):
        validate_approval(
            approval_request(intent),
            intent,
            ApprovalDecision(ApprovalChoice.EDIT),
        )


def test_large_resolver_ranks_and_limits() -> None:
    resolver = SimpleActionResolver()
    task = Task("T1", "publish release document")
    tools = [ActionDescriptor(f"tool_{i}", "irrelevant") for i in range(10)] + [
        ActionDescriptor("publish_document", "publish release document")
    ]
    result = resolver.resolve(task, tools, limit=3, expose_all_below=2)
    assert len(result) == 3
    assert result[0].descriptor.name == "publish_document"
