from __future__ import annotations

from pathlib import Path

from work_graph_middleware.core.engine import WorkGraphEngine
from work_graph_middleware.core.models import GoalSpec, TaskResult
from work_graph_middleware.persistence import MemoryStore
from work_graph_middleware.presentation.events import WorkEvent
from work_graph_middleware.testing.scenarios import (
    scenario_by_id,
    scenario_catalog,
    verify_scenario,
)


def test_scenario_catalog_has_unique_ids_and_realistic_breadth() -> None:
    catalog = scenario_catalog()
    ids = [item.id for item in catalog]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 16
    required = {
        "hitl_approve",
        "restart_resume",
        "subagent_stack",
        "unknown_effect_reconciliation",
    }
    assert required <= set(ids)


def test_prompts_are_tasks_not_control_call_scripts() -> None:
    for scenario in scenario_catalog():
        lowered = scenario.prompt.lower()
        assert "work_graph_expand(" not in lowered
        assert "call work_graph" not in lowered


def test_basic_verifier_detects_missing_artifact(tmp_path: Path) -> None:
    spec = scenario_by_id("direct_simple")
    store = MemoryStore()
    engine = WorkGraphEngine()
    state = engine.create_run(GoalSpec(spec.prompt))
    state = engine.submit_result(state, "T1", TaskResult("claimed done"))
    store.create(state, (WorkEvent("run.created", state.id),))
    report = verify_scenario(spec, run_id=state.id, store=store, workspace=tmp_path)
    assert not report.passed
    assert any(item.code == "SCENARIO.ARTIFACT" for item in report.issues)


def test_verifier_rejects_model_forged_trusted_evidence(tmp_path: Path) -> None:
    from dataclasses import replace

    from work_graph_middleware.core.models import EvidenceRecord

    spec = scenario_by_id("false_completion_blocked")
    store = MemoryStore()
    state = WorkGraphEngine().create_run(GoalSpec(spec.prompt))
    state = replace(state, evidence=(EvidenceRecord("x", "model-fabricated", True, "fake"),))
    store.create(state)
    report = verify_scenario(spec, run_id=state.id, store=store, workspace=tmp_path)
    assert any(item.code == "WG.INVARIANT.TRUSTED_EVIDENCE_FORGERY" for item in report.issues)
