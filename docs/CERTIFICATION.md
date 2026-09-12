# Certification

Coverage is necessary but insufficient for agent middleware. WorkGraph uses two independent gates.

## Deterministic gate

`work-graph-certify` runs:

- pytest with branch coverage >=95%;
- Ruff lint;
- Ruff formatting check;
- strict mypy;
- wheel/sdist build.

These tests own deterministic invariants: graph validity, bounded attempts, exactly-once receipt application, atomic operation claims, persistence/restart semantics, stale invalidation, repair scope, evidence gating and event cursors.

## Live Deep Agents gate

`work-graph-certify --live` uses a real OpenRouter model. Every scenario creates a new DeepAgent and receives a realistic task prompt. The prompt does not tell it which WorkGraph control calls to make. A deterministic fixture injects tools/faults and an independent verifier checks both the final result and the trajectory.

Current scenario families include:

- direct simple work without unnecessary decomposition;
- lazy decomposition and explicit dependency chains;
- tool choice with realistic distractors;
- transient known failure and retry;
- persistent failure followed by local repair;
- false-completion resistance;
- host-only trusted evidence;
- ambiguous external effect, read-only inspection and reconciliation;
- native Deep Agents HITL approve/edit/reject;
- adapter reconstruction + checkpoint resume;
- real Deep Agents `FilesystemBackend` composition;
- real Deep Agents subagent delegation;
- streaming execution;
- durable model-budget failure.

The catalog lives in `work_graph_middleware.testing.scenarios` so prompts and contracts are versioned with the code.

## Artifacts

Each repetition writes a self-contained directory:

```text
scenario/run-01/
  scenario.json
  prompt.md
  model-calls.jsonl
  tool-calls.jsonl
  runner.jsonl
  events.jsonl
  operations.json
  state.json
  final-projection.json
  verification.json
  divergence.json       # failure only
  workspace-manifest.json
  workspace/
  REPORT.md
```

No hidden chain-of-thought is captured. Diagnostics contain only model-visible requests/responses, structured tool calls/results, WorkGraph state/events and host fixture evidence.

`divergence.json` identifies the first failed invariant and classifies it as a model, middleware, integration or environment problem where possible.

## Reliability runs

Use repeated real-model runs for nondeterministic behavior:

```bash
work-graph-certify --live --repetitions 5
```

A planning-quality scenario may be evaluated by pass rate. Safety invariants such as duplicate external effects, approval bypass, completion with unresolved effects and forged trusted evidence are zero-tolerance.

## Credentials

The live runner reads `OPENROUTER_API_KEY` only from the environment or `.env`. It never writes the key into reports. OpenRouter-shaped secrets are redacted defensively from trace values.
