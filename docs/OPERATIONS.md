# Operations and recovery

## Stable diagnostic codes

Public errors use stable `WG.*` codes. Automation and UI code should branch on the code, not exception message text.

Important families:

- `WG.GRAPH.*` — invalid DAG/repair/dependency state;
- `WG.ACTION.*` — authorization, operation or effect problem;
- `WG.TASK.*` — task lifecycle/attempt problem;
- `WG.BUDGET.*` — bounded execution limit;
- `WG.APPROVAL.*` — standalone-runtime approval contract;
- `WG.STORE.*` — persistence and optimistic-revision conflict;
- `WG.INTEGRATION.*` — Deep Agents adapter state.

## Inspect state

```bash
work-graph workgraph.sqlite3 RUN_ID
work-graph workgraph.sqlite3 RUN_ID --events
work-graph workgraph.sqlite3 RUN_ID --events --after-seq 123
```

## Unknown effect

An `UNKNOWN_EFFECT` means WorkGraph knows an operation crossed or may have crossed an external-effect boundary but cannot prove its outcome. Normal domain execution is blocked. Read-only inspection tools can remain available; then reconcile with `happened=true|false`.

Do not automatically choose `false` just to make progress.

## Revision conflicts

Stores use optimistic state revisions. A `WG.STORE.REVISION_CONFLICT` means another worker committed first. Reload durable state and recompute the directive rather than force-writing stale state.

## Schema versioning

Run snapshots and events carry schema versions. New persisted fields must be backward-defaultable or accompanied by an explicit migration. SQLite initialization upgrades operation-journal columns introduced by the 0.3 hardening release.
