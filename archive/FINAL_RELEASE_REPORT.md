# WorkGraph Middleware Final Release Report

## Verdict

**RELEASE READY**

Certification was completed on 2026-09-12 with the repository at commit
`db399d071c419a9dd1e72d9d79abca4c8c431e03` plus the intended working-tree
release fixes. The final live artifact is
`artifacts/certification/20260912-013313`.

## Runtime and dependencies

- Python 3.13.15
- `work-graph-middleware` 0.3.0rc1
- DeepAgents 0.7.13
- LangChain 1.4.0
- LangGraph 1.2.11
- langchain-openai 1.6.2
- Provider: OpenRouter
- Model: `deepseek/deepseek-v4-flash`

## Deterministic gates

- `uv sync --all-extras`: PASS
- `uv run work-graph-certify`: PASS
- Tests: 91 passed
- Branch coverage: 98.13%
- Ruff lint: PASS
- Ruff format check: PASS
- Strict mypy: PASS
- Wheel and sdist build: PASS
- Clean temporary-environment wheel install/import: PASS
- Documentation check: PASS
- Secret scan: PASS

## DeepAgents and live certification

The installed DeepAgents integration contract passed all 5 integration tests.
The final live suite passed **51/51 repetitions** across all 17 scenarios (3
repetitions each; 100% pass rate per scenario):

`direct_simple`, `lazy_decomposition`, `dependency_chain`, `tool_distractors`,
`transient_failure_retry`, `persistent_failure_local_repair`,
`false_completion_blocked`, `trusted_evidence_host`,
`unknown_effect_reconciliation`, `hitl_approve`, `hitl_edit_reauthorize`,
`hitl_reject`, `restart_resume`, `filesystem_backend_stack`, `subagent_stack`,
`streaming_stack`, and `budget_exhaustion`.

## Zero-tolerance safety invariants

All were PASS with zero violations in the final 51 repetitions:

- no duplicate external effects;
- no approval bypass;
- edited HITL arguments were reauthorized;
- no forged trusted evidence;
- no completion with unresolved `UNKNOWN_EFFECT`;
- operation IDs remained unique and replay-safe;
- intent/receipt/reconciliation trajectory remained durable;
- illegal actions were denied;
- dependency ordering and scoped repair were observed.

## Artifacts and observations

Every repetition retained model-visible traces, structured tool calls, ordered
events, operation journal, state/projection, verification, workspace manifest,
and human-readable report. No hidden chain-of-thought or credentials were
captured. The final artifact directory is
`artifacts/certification/20260912-013313`.

The real-model suite is network-latency bound; individual scenario durations
varied materially, while deterministic certification completed in seconds.

## Intentional v1 limitations

WorkGraph remains sequential (`max_concurrency=1`), DeepAgents is the only
supported framework integration, and native DeepAgents owns HITL, checkpoints,
streaming, backends, and subagents. WorkGraph does not add distributed
scheduling, a model router, memory system, MCP gateway, or UI framework.
