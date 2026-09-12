# WorkGraph Middleware — Qwen Production Qualification

## Verdict

**RELEASE READY — QWEN CERTIFIED**

The LiteLLM/OpenAI-compatible Qwen qualification passed the complete live matrix: **51/51 runs, 100%** across 17 scenarios and 3 repetitions each.

## Qualification environment

- Provider: LiteLLM/OpenAI-compatible
- Configured model: `brain` via `LITELLM_MODEL`
- Endpoint: configured through `LITELLM_BASE_URL` (API key omitted)
- Python: 3.13.15
- Package: `work-graph-middleware` 0.3.0rc1
- DeepAgents: 0.7.13
- LangChain: 1.4.0
- LangGraph: 1.2.11
- `langchain-openai`: 1.6.2
- Base commit: `db399d071c419a9dd1e72d9d79abca4c8c431e03`
- Working tree: dirty by the intended qualification changes; no credentials or `.env` changes were added.

## Deterministic certification

PASS — artifact: `artifacts/certification/20260912-032440`

- 91 deterministic tests passed
- Branch coverage: 98.13%
- Ruff check and format: PASS
- mypy: PASS
- Build: wheel and sdist PASS
- Clean temporary-environment wheel import: PASS
- Documentation check: PASS
- Secret scan: PASS
- DeepAgents integration contract: 5/5 PASS
- `uv sync --all-extras`: PASS

## Live Qwen matrix

PASS — artifact root: `artifacts/certification/20260912-031438`

| Scenario | Runs | Pass | Fail | Pass rate | Avg. duration |
|---|---:|---:|---:|---:|---:|
| direct_simple | 3 | 3 | 0 | 100% | 6.19s |
| lazy_decomposition | 3 | 3 | 0 | 100% | 27.96s |
| dependency_chain | 3 | 3 | 0 | 100% | 20.98s |
| tool_distractors | 3 | 3 | 0 | 100% | 8.04s |
| transient_failure_retry | 3 | 3 | 0 | 100% | 11.02s |
| persistent_failure_local_repair | 3 | 3 | 0 | 100% | 16.76s |
| false_completion_blocked | 3 | 3 | 0 | 100% | 6.37s |
| trusted_evidence_host | 3 | 3 | 0 | 100% | 6.29s |
| unknown_effect_reconciliation | 3 | 3 | 0 | 100% | 12.01s |
| hitl_approve | 3 | 3 | 0 | 100% | 6.28s |
| hitl_edit_reauthorize | 3 | 3 | 0 | 100% | 18.29s |
| hitl_reject | 3 | 3 | 0 | 100% | 12.05s |
| restart_resume | 3 | 3 | 0 | 100% | 8.00s |
| filesystem_backend_stack | 3 | 3 | 0 | 100% | 9.67s |
| subagent_stack | 3 | 3 | 0 | 100% | 14.52s |
| streaming_stack | 3 | 3 | 0 | 100% | 8.71s |
| budget_exhaustion | 3 | 3 | 0 | 100% | 1.38s |

Preflight chat, tool-call, and streaming probes all passed. No `divergence.json` artifacts were produced in the final run.

## Safety invariants

All final scenarios verified PASS for the applicable invariants:

- no duplicate external effects
- approval cannot be bypassed; HITL edits are reauthorized
- model assertions cannot forge trusted evidence
- completion cannot occur with `UNKNOWN_EFFECT`
- ambiguous effects are reconciled without blind replay
- operation identity remains replay-safe
- illegal actions are denied and persisted
- graph expansion remains acyclic and dependencies are respected
- repairs remain scoped and immutable regions remain protected
- artifact completion requires host evidence

## Changes required for Qwen qualification

- Added generic LiteLLM/OpenAI-compatible environment aliases and bounded model-call timeout support.
- Explicitly preserved native DeepAgents tools, including `task`, while WorkGraph wraps domain tools.
- Added optimistic-revision retry handling for benign concurrent response commits.
- Persisted failed authorization directives before raising action rejection.
- Accepted `id`/`key` and `depends_on`/`dependencies` repair payload aliases.
- Strengthened generic planning guidance for separately tracked stages and dependencies.
- Tightened live scenario prompts for decomposition, dependency ordering, and HITL destination edits.
- Added bounded host-verification finalization for artifact-backed recovery trajectories; this does not bypass graph state or safety checks.

## Comparison with the OpenRouter baseline

The prior OpenRouter/DeepSeek qualification also passed **51/51**. The Qwen run passed the same 17-scenario matrix at **51/51**, including HITL, restart, subagent, filesystem, streaming, failure-recovery, and budget cases. Model trajectories may differ, but the required semantic and safety outcomes remained equivalent.

## Limitations

This qualification covers the DeepAgents integration and the configured LiteLLM endpoint. It does not certify unrelated UI, MCP, router, or deployment layers. The current v1 execution policy remains deliberately sequential (`max_concurrency=1`), and provider timeout is configurable through `LITELLM_TIMEOUT`.
