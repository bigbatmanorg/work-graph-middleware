from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from work_graph_middleware.core.models import RunState
from work_graph_middleware.errors import RevisionConflict, WorkGraphError
from work_graph_middleware.persistence.base import OperationClaim, OperationRecord, OperationState
from work_graph_middleware.persistence.codec import state_from_json, state_to_json
from work_graph_middleware.presentation.events import WorkEvent


class SQLiteStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    run_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    semantics_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'reserved',
                    execution_count INTEGER NOT NULL DEFAULT 0,
                    reserved_at REAL NOT NULL DEFAULT 0,
                    resolution TEXT,
                    PRIMARY KEY(run_id, operation_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )
            self._ensure_operation_columns(conn)

    @staticmethod
    def _ensure_operation_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(operations)").fetchall()}
        additions = {
            "state": "TEXT NOT NULL DEFAULT 'reserved'",
            "execution_count": "INTEGER NOT NULL DEFAULT 0",
            "reserved_at": "REAL NOT NULL DEFAULT 0",
            "resolution": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE operations ADD COLUMN {name} {ddl}")

    @staticmethod
    def _event_json(event: WorkEvent) -> str:
        return json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))

    def _insert_events(
        self, conn: sqlite3.Connection, state: RunState, events: tuple[WorkEvent, ...]
    ) -> None:
        for event in events:
            cursor = conn.execute(
                "INSERT INTO events(run_id, event_json) VALUES (?, ?)",
                (state.id, "{}"),
            )
            if cursor.lastrowid is None:
                raise WorkGraphError(
                    "WG.STORE.EVENT_INSERT", "SQLite did not return an event sequence"
                )
            seq = cursor.lastrowid
            stored = event.stored(
                seq=seq,
                state_revision=state.revision,
                graph_revision=state.graph_revision,
            )
            conn.execute(
                "UPDATE events SET event_json=? WHERE seq=?",
                (self._event_json(stored), seq),
            )

    def create(self, state: RunState, events: tuple[WorkEvent, ...] = ()) -> None:
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id, revision, state_json) VALUES (?, ?, ?)",
                    (state.id, state.revision, state_to_json(state)),
                )
                self._insert_events(conn, state, events)
        except sqlite3.IntegrityError as exc:
            raise WorkGraphError("WG.STORE.ALREADY_EXISTS", state.id) from exc

    def load(self, run_id: str) -> RunState:
        with self._connection() as conn:
            row = conn.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkGraphError("WG.STORE.NOT_FOUND", run_id)
        return state_from_json(str(row[0]))

    def commit(
        self, state: RunState, *, expected_revision: int, events: tuple[WorkEvent, ...] = ()
    ) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET revision=?, state_json=? WHERE run_id=? AND revision=?",
                (state.revision, state_to_json(state), state.id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict()
            self._insert_events(conn, state, events)

    def events(self, run_id: str, *, after_seq: int = 0) -> tuple[WorkEvent, ...]:
        self.load(run_id)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT seq, event_json FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        result: list[WorkEvent] = []
        for seq, raw in rows:
            event = WorkEvent.from_dict(json.loads(str(raw)))
            if event.seq is None:
                event = event.stored(
                    seq=int(seq),
                    state_revision=event.state_revision or 0,
                    graph_revision=event.graph_revision or 0,
                )
            result.append(event)
        return tuple(result)

    def run_ids(self) -> tuple[str, ...]:
        with self._connection() as conn:
            rows = conn.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
        return tuple(str(row[0]) for row in rows)

    def claim_operation(
        self, run_id: str, operation_id: str, semantics_digest: str
    ) -> OperationClaim:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO operations("
                "run_id,operation_id,semantics_digest,result_json,state,"
                "execution_count,reserved_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    operation_id,
                    semantics_digest,
                    "",
                    OperationState.RESERVED.value,
                    0,
                    time.time(),
                ),
            )
            acquired = cursor.rowcount == 1
            row = conn.execute(
                "SELECT semantics_digest,result_json,state,execution_count,reserved_at,resolution "
                "FROM operations WHERE run_id=? AND operation_id=?",
                (run_id, operation_id),
            ).fetchone()
        if row is None:
            raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
        if str(row[0]) != semantics_digest:
            raise WorkGraphError("WG.ACTION.OPERATION_CONFLICT", operation_id)
        return OperationClaim(acquired, self._operation_from_row(operation_id, row))

    @staticmethod
    def _operation_from_row(operation_id: str, row: tuple[object, ...]) -> OperationRecord:
        execution_count = row[3]
        reserved_at = row[4]
        if not isinstance(execution_count, int) or not isinstance(reserved_at, (int, float)):
            raise WorkGraphError("WG.STORE.OPERATION_CORRUPT", operation_id)
        return OperationRecord(
            operation_id=operation_id,
            semantics_digest=str(row[0]),
            result_json=str(row[1] or ""),
            state=OperationState(str(row[2])),
            execution_count=execution_count,
            reserved_at=float(reserved_at),
            resolution=str(row[5]) if row[5] is not None else None,
        )

    def get_operation(self, run_id: str, operation_id: str) -> OperationRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT semantics_digest,result_json,state,execution_count,reserved_at,resolution "
                "FROM operations WHERE run_id=? AND operation_id=?",
                (run_id, operation_id),
            ).fetchone()
        return None if row is None else self._operation_from_row(operation_id, row)

    def complete_operation(
        self, run_id: str, operation_id: str, semantics_digest: str, result_json: str
    ) -> OperationRecord:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT semantics_digest,result_json,state,execution_count,reserved_at,resolution "
                "FROM operations WHERE run_id=? AND operation_id=?",
                (run_id, operation_id),
            ).fetchone()
            if row is None:
                raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
            previous = self._operation_from_row(operation_id, row)
            if previous.semantics_digest != semantics_digest:
                raise WorkGraphError("WG.ACTION.OPERATION_CONFLICT", operation_id)
            if previous.result_json and previous.result_json != result_json:
                raise WorkGraphError("WG.ACTION.OPERATION_RESULT_CONFLICT", operation_id)
            conn.execute(
                "UPDATE operations SET result_json=?,state=?,execution_count=? "
                "WHERE run_id=? AND operation_id=?",
                (
                    result_json,
                    OperationState.COMPLETED.value,
                    max(1, previous.execution_count),
                    run_id,
                    operation_id,
                ),
            )
        result = self.get_operation(run_id, operation_id)
        assert result is not None
        return result

    def resolve_operation(
        self, run_id: str, operation_id: str, *, resolution: str
    ) -> OperationRecord:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE operations SET state=?,resolution=? WHERE run_id=? AND operation_id=?",
                (OperationState.RESOLVED.value, resolution, run_id, operation_id),
            )
            if cursor.rowcount != 1:
                raise WorkGraphError("WG.ACTION.UNKNOWN_OPERATION", operation_id)
        result = self.get_operation(run_id, operation_id)
        assert result is not None
        return result

    def operations(self, run_id: str) -> tuple[OperationRecord, ...]:
        self.load(run_id)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT "
                "operation_id,semantics_digest,result_json,state,execution_count,"
                "reserved_at,resolution "
                "FROM operations WHERE run_id=? ORDER BY reserved_at,operation_id",
                (run_id,),
            ).fetchall()
        return tuple(
            OperationRecord(
                operation_id=str(row[0]),
                semantics_digest=str(row[1]),
                result_json=str(row[2] or ""),
                state=OperationState(str(row[3])),
                execution_count=int(row[4]),
                reserved_at=float(row[5]),
                resolution=str(row[6]) if row[6] is not None else None,
            )
            for row in rows
        )
