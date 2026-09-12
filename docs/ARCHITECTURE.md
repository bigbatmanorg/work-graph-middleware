# Architecture

## Boundary

```text
Deep Agents runtime
  model / messages / checkpoint / interrupts / subagents / backends
                 │
                 ▼
        WorkGraphMiddleware
  filters legal domain tools, journals effects,
  exposes semantic control tools, emits progress
                 │
                 ▼
        deterministic core
  graph + scheduler + policy + evidence + recovery
                 │
                 ▼
        WorkGraphStore
  state revisions + event stream + operation journal
```

The model proposes work. The host owns truth.

## Semantic state

A run contains a rooted DAG of semantic tasks. Tasks may be pending, ready, waiting, completed, stale, failed or superseded. Expansion is lazy. Completed graph regions are immutable under repair; changed upstream results invalidate completed descendants so they must be recomputed.

## External-effect protocol

Domain actions use a durable boundary:

1. WorkGraph authorizes the exact current tool + arguments.
2. The store atomically claims an operation attempt before execution.
3. The tool executes through the ordinary Deep Agents tool pipeline.
4. A success, known failure, or ambiguous outcome becomes a durable receipt.
5. The receipt is applied to semantic state exactly once.

A reserved operation without a durable receipt is treated as `UNKNOWN_EFFECT`. It is never blindly replayed. A read-only observation plus reconciliation can establish whether the effect happened.

Known failures are safe to retry. For Deep Agents retry middleware, WorkGraph derives attempt-specific operation IDs from the stable LangChain tool-call ID; a proven failure can create another attempt while a success/ambiguous attempt is replayed rather than executed again.

## Failure and repair

Repeated action failures reach `WG.TASK.MAX_ATTEMPTS` and persist run/task failure. Recovery has two explicit choices:

- retry the failed run, resetting that task's bounded attempt window; or
- apply a scoped `GraphPatch` that supersedes only unfinished work inside a selected region and adds replacement children.

Global replanning is deliberately not a primitive.

## Evidence

A model can submit semantic results and ordinary evidence references. Trusted acceptance evidence is host-only through `record_trusted_evidence`; there is intentionally no model-facing trusted-evidence tool.

## Presentation

The full `project()` output is for UI/debugging. `model_context()` is deliberately smaller: current task, dependency results, local children, legal actions, run status and unresolved effects. The full DAG is not injected on every model turn.

Events are schema-versioned and receive a monotonic store sequence, event ID, state/graph revision and optional operation/correlation metadata. Consumers can resume with `events(after_seq=...)`.
