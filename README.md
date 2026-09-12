# Work Graph Middleware

Durable, evidence-driven semantic work orchestration for **Deep Agents**.

WorkGraph is not another workflow DSL and it does not replace Deep Agents or LangGraph. Deep Agents owns agent execution, conversation state, checkpointing, interrupts, filesystem backends and subagents. WorkGraph owns the semantic work state: what remains to be done, dependencies, legal actions, external-effect accounting, recovery, and auditable progress.

## Why

LLMs are useful planners but weak authorities. A model can forget a dependency, declare success too early, retry an ambiguous side effect, or rewrite a plan after partial progress. WorkGraph keeps those decisions inside a small deterministic state machine while leaving ordinary Deep Agents tools and middleware intact.

Core guarantees:

- lazy semantic task decomposition rather than mandatory up-front plans;
- deterministic dependency scheduling and scoped repair;
- host-authoritative completion and trusted evidence;
- durable intent/receipt journal around external effects;
- atomic operation claiming and replay-safe receipt application;
- explicit `UNKNOWN_EFFECT` reconciliation instead of blind retry;
- bounded retries and durable failure/retry state;
- stale downstream invalidation when upstream results change;
- native Deep Agents HITL composition (approve/edit/reject);
- stable, ordered, cursor-friendly progress events;
- compact model context separate from the full UI/debug projection;
- detailed deterministic and real-model certification artifacts.

## Scope

The supported agent integration is **Deep Agents**. The deterministic core is framework-independent for testing and embedding, but there is intentionally no generic LangGraph adapter. LangGraph behavior is exercised through Deep Agents checkpoint, interrupt and middleware semantics.

WorkGraph v1 intentionally supports a single semantic action at a time (`max_concurrency=1`). Multiple independent Deep Agents runs can execute concurrently; one WorkGraph run does not pretend to provide distributed task scheduling.

## Install

```bash
pip install 'work-graph-middleware[deepagents]'
```

Development:

```bash
uv sync --all-extras
```

## Deep Agents usage

```python
from deepagents import create_deep_agent
from work_graph_middleware.integrations.deepagents import WorkGraphMiddleware

work_graph = WorkGraphMiddleware()

agent = create_deep_agent(
    model=model,
    tools=tools,
    middleware=[work_graph],
)
```

Or use the convenience constructor:

```python
from work_graph_middleware.integrations.deepagents import create_work_graph_agent

agent = create_work_graph_agent(model=model, tools=tools)
```

### Native HITL

Declare WorkGraph policy metadata and let Deep Agents own the actual interrupt/checkpoint UX:

```python
from langgraph.checkpoint.memory import MemorySaver
from work_graph_middleware.integrations.deepagents import (
    ToolPolicy,
    WorkGraphMiddleware,
    create_work_graph_agent,
)

work_graph = WorkGraphMiddleware(
    tool_policies={
        "publish_document": ToolPolicy(
            destructive=True,
            requires_approval=True,
        )
    }
)

agent = create_work_graph_agent(
    model=model,
    tools=[publish_document],
    work_graph_middleware=work_graph,
    checkpointer=MemorySaver(),
)
```

The convenience constructor translates `requires_approval=True` into Deep Agents `interrupt_on`. Approve executes the original call; edit reaches WorkGraph as a new final argument set and is re-authorized/journaled; reject never crosses WorkGraph's effect boundary.

## Persistence

Zero-config development uses `MemoryStore`. Durable local use:

```python
from work_graph_middleware.integrations.deepagents import WorkGraphMiddleware
from work_graph_middleware.persistence import SQLiteStore

middleware = WorkGraphMiddleware(store=SQLiteStore("workgraph.sqlite3"))
```

Stores implement optimistic run revisions, ordered events, and atomic operation claims. The protocol is intentionally small so a production Postgres implementation can be supplied without changing the engine.

## Real-model certification with OpenRouter

Copy `.env.example` to `.env` and set a development key:

```bash
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
```

Run deterministic release gates:

```bash
work-graph-certify
```

Run the complete real Deep Agents suite:

```bash
work-graph-certify --live --repetitions 3
```

Run one failure reproduction repeatedly:

```bash
work-graph-certify --live \
  --only unknown_effect_reconciliation \
  --repetitions 5
```

Each live scenario creates a fresh DeepAgent and receives a practical goal, not a scripted list of WorkGraph calls. The independent verifier checks final effects and the WorkGraph journey. Results are written under `artifacts/certification/` and include model-visible request/response summaries, actual tool calls, ordered WorkGraph events, operation journal, final state, workspace hashes, verification, first divergence, and `REPORT.md`. Credentials are never written to reports.

See [docs/CERTIFICATION.md](docs/CERTIFICATION.md).

## Support contract

The 0.3 line targets:

- Python 3.11–3.13;
- Deep Agents `>=0.7.13,<0.8`;
- LangChain `>=1.4,<2` and LangGraph `>=1.0,<2` as required by the Deep Agents integration;
- sync and async middleware hooks;
- Memory and SQLite WorkGraph stores;
- Deep Agents filesystem backend, subagents, HITL/checkpoint resume, streaming and custom middleware composition as certified scenarios.

Release readiness is defined by the certification gates, not by code coverage alone.

## Design documents

- [Architecture](docs/ARCHITECTURE.md)
- [Deep Agents integration](docs/DEEPAGENTS.md)
- [Certification and diagnostics](docs/CERTIFICATION.md)
- [Operations and recovery](docs/OPERATIONS.md)
