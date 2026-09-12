from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from work_graph_middleware.core.engine import WorkGraphEngine
from work_graph_middleware.core.models import GoalSpec
from work_graph_middleware.errors import RevisionConflict, WorkGraphError
from work_graph_middleware.persistence import MemoryStore, SQLiteStore
from work_graph_middleware.persistence.codec import state_from_json, state_to_json
from work_graph_middleware.presentation.events import WorkEvent


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        return MemoryStore()
    return SQLiteStore(tmp_path / "state.db")


def test_state_codec_roundtrip() -> None:
    state = WorkGraphEngine().create_run(GoalSpec("round trip"))
    restored = state_from_json(state_to_json(state))
    assert restored == state


def test_event_sequences_and_cursor(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("events"))
    store.create(state, (WorkEvent("one", state.id),))
    updated = state.evolve()
    store.commit(updated, expected_revision=state.revision, events=(WorkEvent("two", state.id),))
    events = store.events(state.id)
    assert [item.type for item in events] == ["one", "two"]
    assert [item.seq for item in events] == sorted(item.seq for item in events)
    assert [item.type for item in store.events(state.id, after_seq=events[0].seq or 0)] == ["two"]
    assert all(item.state_revision is not None for item in events)


def test_optimistic_revision_conflict(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("revision"))
    store.create(state)
    store.commit(state.evolve(), expected_revision=state.revision)
    with pytest.raises(RevisionConflict):
        store.commit(state.evolve(), expected_revision=state.revision)


def test_operation_claim_is_atomic_for_memory_and_sqlite(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("claim"))
    store.create(state)

    def claim():
        return store.claim_operation(state.id, "op", "same")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: claim(), range(20)))
    assert sum(item.acquired for item in claims) == 1


def test_operation_semantics_conflict_rejected(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("claim"))
    store.create(state)
    store.claim_operation(state.id, "op", "one")
    with pytest.raises(WorkGraphError, match="WG.ACTION.OPERATION_CONFLICT"):
        store.claim_operation(state.id, "op", "two")


def test_operation_complete_and_resolve(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("claim"))
    store.create(state)
    store.claim_operation(state.id, "op", "semantics")
    completed = store.complete_operation(state.id, "op", "semantics", '{"x":1}')
    assert completed.execution_count == 1
    assert completed.result_json == '{"x":1}'
    resolved = store.resolve_operation(state.id, "op", resolution="happened")
    assert resolved.resolution == "happened"
    assert store.operations(state.id)[0].operation_id == "op"


def test_duplicate_create_and_missing_load_raise(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("x"))
    store.create(state)
    with pytest.raises(WorkGraphError, match="WG.STORE.ALREADY_EXISTS"):
        store.create(state)
    with pytest.raises(WorkGraphError, match="WG.STORE.NOT_FOUND"):
        store.load("missing")


def test_run_ids_and_missing_operation(store) -> None:
    a = WorkGraphEngine().create_run(GoalSpec("a"))
    b = WorkGraphEngine().create_run(GoalSpec("b"))
    store.create(a)
    store.create(b)
    assert set(store.run_ids()) == {a.id, b.id}
    assert store.get_operation(a.id, "missing") is None


def test_complete_unknown_operation_and_conflicting_result_raise(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("x"))
    store.create(state)
    with pytest.raises(WorkGraphError, match="WG.ACTION.UNKNOWN_OPERATION"):
        store.complete_operation(state.id, "missing", "s", "{}")
    store.claim_operation(state.id, "op", "s")
    store.complete_operation(state.id, "op", "s", '{"a":1}')
    with pytest.raises(WorkGraphError, match="WG.ACTION.OPERATION_RESULT_CONFLICT"):
        store.complete_operation(state.id, "op", "s", '{"a":2}')
    with pytest.raises(WorkGraphError, match="WG.ACTION.OPERATION_CONFLICT"):
        store.complete_operation(state.id, "op", "other", '{"a":1}')


def test_resolve_unknown_operation_raises(store) -> None:
    state = WorkGraphEngine().create_run(GoalSpec("x"))
    store.create(state)
    with pytest.raises(WorkGraphError, match="WG.ACTION.UNKNOWN_OPERATION"):
        store.resolve_operation(state.id, "missing", resolution="no")


def test_memory_claim_requires_existing_run() -> None:
    store = MemoryStore()
    with pytest.raises(WorkGraphError, match="WG.STORE.NOT_FOUND"):
        store.claim_operation("missing", "op", "s")


def test_sqlite_migrates_legacy_operation_columns(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          revision INTEGER NOT NULL,
          state_json TEXT NOT NULL
        );
        CREATE TABLE events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          event_json TEXT NOT NULL
        );
        CREATE TABLE operations (
          run_id TEXT NOT NULL,
          operation_id TEXT NOT NULL,
          semantics_digest TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(run_id, operation_id)
        );
        """
    )
    connection.close()
    SQLiteStore(path)
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
    connection.close()
    assert {"state", "execution_count", "reserved_at", "resolution"} <= columns


def test_sqlite_reads_legacy_event_without_embedded_seq(tmp_path: Path) -> None:
    import json
    import sqlite3

    path = tmp_path / "legacy-events.db"
    store = SQLiteStore(path)
    state = WorkGraphEngine().create_run(GoalSpec("x"))
    store.create(state)
    raw = WorkEvent("legacy", state.id).to_dict()
    raw["seq"] = None
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO events(run_id,event_json) VALUES (?,?)",
        (state.id, json.dumps(raw)),
    )
    connection.commit()
    connection.close()
    event = store.events(state.id)[0]
    assert event.seq is not None


def test_codec_handles_no_result_and_old_missing_fields() -> None:
    import json

    from work_graph_middleware.persistence.codec import state_from_json, state_to_json

    state = WorkGraphEngine().create_run(GoalSpec("x"))
    data = json.loads(state_to_json(state))
    data["tasks"]["T1"].pop("host_action_attempts")
    data["tasks"]["T1"].pop("failure_count")
    data["tasks"]["T1"].pop("last_failure_code")
    data["counters"].pop("effects_applied")
    data.pop("applied_operations")
    restored = state_from_json(json.dumps(data))
    assert restored.tasks["T1"].result is None
    assert restored.tasks["T1"].host_action_attempts == 0
