from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("langchain")

from work_graph_middleware.integrations.deepagents import (
    CONTROL_TOOL_NAMES,
    ToolPolicy,
    WorkGraphMiddleware,
    create_work_graph_agent,
)

pytestmark = pytest.mark.integration


def test_control_tool_surface_is_complete() -> None:
    middleware = WorkGraphMiddleware()
    assert {tool.name for tool in middleware.tools} == CONTROL_TOOL_NAMES


def test_before_agent_stores_run_id_in_state_update() -> None:
    middleware = WorkGraphMiddleware()
    update = middleware.before_agent(
        {"messages": [{"role": "user", "content": "build artifact"}]},
        runtime=None,
    )
    assert update is not None
    run_id = update["work_graph_run_id"]
    assert middleware.store.load(run_id).goal.objective == "build artifact"


def test_existing_run_id_is_reused_not_recreated() -> None:
    middleware = WorkGraphMiddleware()
    update = middleware.before_agent(
        {"messages": [{"role": "user", "content": "task"}]},
        runtime=None,
    )
    assert update is not None
    run_id = update["work_graph_run_id"]
    assert middleware.before_agent({"work_graph_run_id": run_id}, runtime=None) is None
    assert middleware.latest_run_id == run_id


def test_convenience_constructor_translates_workgraph_approval_policy(monkeypatch) -> None:
    import deepagents

    captured = {}

    def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(args=args, kwargs=kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create)
    middleware = WorkGraphMiddleware(tool_policies={"publish": ToolPolicy(requires_approval=True)})
    create_work_graph_agent(model="fake", tools=[], work_graph_middleware=middleware)
    assert captured["interrupt_on"]["publish"]["allowed_decisions"] == [
        "approve",
        "edit",
        "reject",
    ]
    assert captured["middleware"][0] is middleware


def test_explicit_interrupt_policy_wins(monkeypatch) -> None:
    import deepagents

    captured = {}

    monkeypatch.setattr(
        deepagents,
        "create_deep_agent",
        lambda *args, **kwargs: captured.update(kwargs) or object(),
    )
    middleware = WorkGraphMiddleware(tool_policies={"publish": ToolPolicy(requires_approval=True)})
    create_work_graph_agent(
        model="fake",
        tools=[],
        work_graph_middleware=middleware,
        interrupt_on={"publish": {"allowed_decisions": ["approve"]}},
    )
    assert captured["interrupt_on"]["publish"] == {"allowed_decisions": ["approve"]}
