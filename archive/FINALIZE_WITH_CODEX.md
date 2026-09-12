# Goal Prompt — Finalize WorkGraph Middleware to Release Quality

You are the final release engineer for this repository. Work autonomously until the repository is genuinely release-ready. Do not stop at analysis, do not merely report failures, and do not ask for confirmation for routine fixes. Investigate, modify, test, rerun, and iterate until all required gates and live scenarios are green or a remaining failure is demonstrably external to this repository.

## Product scope — do not expand it

WorkGraph is **DeepAgents-first work orchestration middleware**. It is not a generic workflow engine, not a separate LangGraph product, not a model router, memory system, MCP gateway, distributed scheduler, or UI framework.

The intended architecture is:

- framework-independent deterministic WorkGraph core;
- one supported framework integration: DeepAgents;
- LangGraph compatibility matters only through the DeepAgents runtime family (state, checkpoints, interrupts, resume, streaming, middleware composition, subagents, backends);
- host-authoritative semantic work state;
- durable side-effect intent/receipt handling;
- evidence-gated completion;
- bounded retries and local repair;
- UNKNOWN_EFFECT reconciliation instead of blind replay;
- observable, versioned event/projection surfaces suitable for a future UI;
- v1 execution remains deliberately sequential. Do not add distributed scheduling or parallel execution just to make `max_concurrency` exist.

Read these before changing anything:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DEEPAGENTS.md`
- `docs/CERTIFICATION.md`
- `docs/OPERATIONS.md`

## Current known state

Before packaging, the deterministic source was verified with:

```text
86 passed
1 DeepAgents integration module skipped because DeepAgents was unavailable in that execution environment
98.40% deterministic-core branch coverage
secret scan PASS
```

The previous environment could not install/run Ruff, mypy, build tooling, DeepAgents/LangChain, or call the real LLM endpoint. Your job is to finish those gates in a normal development environment.

Do not weaken the quality gates or live assertions to obtain green results. Fix the implementation, integration, fixture, or prompt when a test exposes a real problem.

## Environment and credentials

The user will populate `.env` before giving you this goal.

The live harness currently supports OpenRouter-compatible settings:

```env
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_TEMPERATURE=0
OPENROUTER_HTTP_REFERER=...
```

Rules:

1. Read credentials only from environment/`.env`.
2. Never print complete API keys.
3. Never commit `.env`, secrets, request headers containing credentials, or generated traces containing secrets.
4. Keep the existing redaction/secret scan effective.
5. If the user's `.env` instead contains a different OpenAI-compatible development backend, minimally generalize the live model configuration while preserving the existing `OPENROUTER_*` interface as a supported alias. Do not redesign the package around providers.

## Phase 1 — establish the real environment

Use `uv` as the canonical environment/package workflow.

Run, and fix any packaging/dependency issues discovered:

```bash
uv sync --all-extras
```

Generate/update `uv.lock` if appropriate and commit it if the repository is intended to use frozen/reproducible installs.

Confirm imports for the supported DeepAgents stack and inspect the actually installed public APIs rather than assuming signatures from memory.

The supported version contract in `pyproject.toml` must be internally consistent and tested. Prefer a narrow, explicit compatibility range over loose claims.

## Phase 2 — make every deterministic release gate genuinely green

Run the canonical gate without development exceptions:

```bash
uv run work-graph-certify
```

Also run individual gates while diagnosing failures:

```bash
uv run pytest
uv run pytest --cov=work_graph_middleware --cov-branch --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m build
uv run python scripts/docs_check.py
uv run python scripts/secret_scan.py
```

Requirements:

- all deterministic tests pass;
- >=95% branch coverage for the deterministic core as configured;
- Ruff lint PASS;
- Ruff format PASS;
- strict mypy PASS;
- wheel + sdist build PASS;
- built wheel can be installed into a clean temporary environment and imported;
- docs check PASS;
- secret scan PASS;
- no ignored exception is used to hide a real type or correctness problem unless tightly justified at a framework boundary;
- public package remains `py.typed`.

If static checking finds problems in `integrations/deepagents.py` or the live-testing harness, fix them rather than excluding large files from mypy/lint.

## Phase 3 — validate DeepAgents integration against real installed APIs

This is mandatory. WorkGraph must behave correctly as normal DeepAgents middleware without forking or monkey-patching DeepAgents.

Verify at minimum:

- middleware creation through `create_deep_agent`;
- custom state carrying `work_graph_run_id` safely across a run;
- sync and async hooks;
- model wrapping and dynamic visible-tool filtering;
- tool wrapping and final-call authorization;
- streaming execution;
- checkpoint/resume/reconstruction semantics;
- coexistence with DeepAgents filesystem/backend behavior;
- coexistence with subagents;
- coexistence with native HITL/interruption;
- no mutable middleware-global field is semantic authority for concurrent threads/runs;
- no normal DeepAgents behavior is broken by installing WorkGraph.

### HITL rule

Do not build a second competing human-approval runtime inside WorkGraph.

DeepAgents/LangChain native HITL owns pause/approve/edit/reject UX. WorkGraph must authorize and journal the final call that actually reaches execution. An edited call must therefore be re-evaluated against WorkGraph policy/candidate legality and cannot inherit stale authorization for old arguments.

Add/fix integration tests proving approve, reject, and edited-arguments behavior end to end.

## Phase 4 — audit the core invariants before live testing

Make sure the implementation and tests prove all of these. Add focused regression tests if any are missing.

### Graph/work lifecycle

- graph stays acyclic and bounded;
- lazy decomposition, not mandatory giant upfront plans;
- dependencies gate readiness;
- expanded parents resume only after active children finish/supersede;
- completed descendants become stale when upstream output materially changes;
- repair is scoped/local and cannot casually rewrite unrelated completed work;
- bounded task attempts transition into a durable failure state;
- failed runs are explicitly persisted and safely retryable only when appropriate;
- completion requires the root, requirement coverage, trusted evidence requirements, and no unresolved effects.

### Side effects

- operation intent/claim exists before crossing external side-effect boundary;
- the same operation ID + same semantics never executes the external effect twice;
- conflicting reuse of operation ID fails;
- cached receipt replay does not increment semantic success/tool counters again;
- concurrent workers cannot both claim the same external effect;
- intent-without-receipt after restart becomes `UNKNOWN_EFFECT` rather than blind retry;
- reconciliation records whether the effect happened and resumes safely.

### Evidence

- model claims are not trusted evidence;
- trusted evidence enters only through a host/verifier path;
- fake evidence cannot complete the run;
- final outcome verification checks real effects/artifacts, not prose.

### Events/observability

- events have stable schema version;
- durable monotonic sequence/cursor behavior works;
- event records are sufficient for UI/debug replay;
- operation/task/run identifiers are preserved where useful;
- persisted state has a schema version and migration behavior is explicit for v1.

## Phase 5 — run the real LLM certification suite

This is a hard release requirement, not a demo.

Use the actual `.env` model backend and run:

```bash
uv run work-graph-certify --live --repetitions 3
```

For difficult/non-safety planning scenarios, increase to 5 repetitions if needed to establish whether failures are model variance or middleware bugs.

Do not tell the model which internal WorkGraph calls it must make unless the scenario explicitly tests the control API itself. Prompts should describe realistic jobs; the middleware behavior must emerge from the stack.

The catalog currently covers approximately these behavioral families and should remain focused rather than ballooning into hundreds of tests:

1. trivial direct work without unnecessary decomposition;
2. lazy decomposition;
3. dependency ordering;
4. tool selection with realistic distractors;
5. invalid/illegal action recovery;
6. transient known failure and retry;
7. persistent failure followed by scoped local repair;
8. false-completion resistance;
9. host-only trusted evidence / fake-evidence rejection;
10. ambiguous external effect / UNKNOWN_EFFECT;
11. reconciliation and safe continuation;
12. HITL approve;
13. HITL edit with reauthorization;
14. HITL reject;
15. restart/checkpoint/resume or middleware reconstruction;
16. real DeepAgents filesystem/backend composition;
17. real DeepAgents subagent composition;
18. streaming observability where represented by the current scenario catalog;
19. model-budget exhaustion / bounded wandering where represented by the current scenario catalog.

Do not add scenarios only to inflate the count. Every scenario must have a distinct behavioral contract.

## Phase 6 — independently verify the journey, not just the output

Every live scenario must prove both the final effect and the WorkGraph trajectory.

Examples of required checks:

- expected file/object truly exists and has correct content/hash;
- real tool call occurred;
- intent preceded effect;
- receipt followed or reconciled the effect;
- dependency was actually respected;
- illegal action was denied;
- approval occurred before sensitive execution;
- edited HITL arguments were reauthorized;
- retry did not duplicate external effect;
- repair touched only the intended graph region;
- downstream stale state was created after upstream change;
- root did not complete while children/evidence/effects remained unresolved;
- trusted evidence originated from the host verifier;
- completed run cannot contain UNKNOWN_EFFECT.

Negative assertions are mandatory for safety-sensitive cases.

## Phase 7 — preserve detailed diagnostic artifacts

Do not reduce artifact detail to make tests simpler.

Each repetition should remain self-contained and useful to a human or another coding agent. Preserve/generate artifacts such as:

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
  divergence.json       # on failure
  workspace-manifest.json
  workspace/
  REPORT.md
```

Do **not** capture hidden chain-of-thought. Capture only model-visible requests/responses, structured tool calls/results, WorkGraph decisions/state/events, verifier observations, timings and safe metadata.

On failure, `divergence.json`/`REPORT.md` should identify the earliest violated invariant and classify the failure when possible as one of:

- middleware bug;
- DeepAgents integration bug;
- model-quality failure;
- fixture/verifier bug;
- environment/provider failure.

Do not classify a failure as “model” merely because a model was involved. If WorkGraph allowed a forbidden state transition or unsafe side effect, that is a middleware failure.

## Phase 8 — test realistic DeepAgents stacks, not WorkGraph in isolation

This is non-optional.

At minimum, certify representative combinations that can materially interact with WorkGraph:

- WorkGraph + normal domain tools;
- WorkGraph + DeepAgents filesystem/backend;
- WorkGraph + subagents;
- WorkGraph + native HITL;
- WorkGraph + checkpoint/resume;
- WorkGraph + streaming;
- WorkGraph + another ordinary custom middleware if useful for ordering tests;
- a combined realistic stack where appropriate (for example checkpoint + HITL + WorkGraph, or filesystem + subagents + WorkGraph).

Do not test the entire Cartesian product. Test interaction boundaries that can change semantics.

## Phase 9 — prompts are part of the product

Spend real effort improving scenario prompts when they fail for prompt ambiguity rather than middleware behavior.

A good live prompt:

- describes a realistic user objective;
- provides enough context to solve it;
- does not leak verifier internals;
- does not explicitly order internal WorkGraph API calls;
- naturally forces the target behavior through the fixture/environment;
- is stable enough for repeated model runs;
- allows semantically equivalent good plans rather than exact task-name matching.

When a scenario is flaky:

1. inspect the full artifact trail;
2. identify the first divergence;
3. determine whether prompt ambiguity, fixture design, resolver/tool exposure, middleware policy, or model behavior caused it;
4. fix the correct layer;
5. rerun that scenario repeatedly;
6. rerun the full suite afterward.

## Phase 10 — keep the model context efficient

Do not inject the full UI/debug graph into every model call.

The model-facing context should remain compact and task-local: current task, relevant dependency results/facts, legal/current actions, immediate blockers/children, concise run status. Full projections/events are for UI/debugging/artifacts.

Measure live prompt/tool-schema size when practical and report obvious regressions.

## Phase 11 — final release gate

After fixes, run from a clean checkout/environment if possible:

```bash
uv sync --all-extras
uv run work-graph-certify
uv run work-graph-certify --live --repetitions 3
```

Then verify:

- clean `git status` except intended changes/artifacts excluded by `.gitignore`;
- no credentials in git history/diff/current tree;
- package build/install works;
- docs describe behavior that actually exists;
- README quickstart is executable;
- supported version range is accurate;
- workflows are valid;
- generated live reports are ignored from source control unless intentionally included as a sanitized sample;
- no legacy/v1/v2 duplicate implementation remains;
- no dead config field claims unsupported behavior;
- no stale files describe the old architecture.

## Definition of done

Do not declare release-ready until all of the following are true:

1. deterministic quality/correctness certification is fully green with no “UNAVAILABLE” gates;
2. installed DeepAgents integration tests are green;
3. the real LLM live suite is green at the required repetition rate;
4. zero-tolerance safety invariants have zero violations across repetitions;
5. realistic DeepAgents stack scenarios are green;
6. failure artifacts are detailed enough to diagnose a regression without reproducing it manually;
7. docs and public APIs match the implementation;
8. secrets are absent;
9. build/install from source succeeds;
10. the repository is clean and publishable.

## Final deliverable

When complete, create `FINAL_RELEASE_REPORT.md` containing:

- exact dependency/runtime versions tested;
- commit SHA;
- deterministic gate results;
- branch coverage;
- DeepAgents integration matrix and results;
- live model/provider/model identifier (never credentials);
- each live scenario + repetition count + pass rate;
- explicit list of zero-tolerance safety invariants and whether any were violated;
- artifact directory for the final certification run;
- performance/token observations worth knowing;
- intentional v1 limitations/non-goals;
- final verdict: `RELEASE READY` or `NOT RELEASE READY`.

If anything remains red, continue working. Only use `NOT RELEASE READY` when the remaining blocker is genuinely outside your ability to fix in this repository, and document exact evidence and the minimum external action required.
