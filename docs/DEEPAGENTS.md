# Deep Agents integration

Deep Agents is the only supported agent framework integration.

## Ownership

Deep Agents owns:

- model invocation and message state;
- LangGraph thread checkpoints and resume;
- native HITL interrupts;
- filesystem backends;
- subagent delegation;
- streaming and other Deep Agents middleware.

WorkGraph owns:

- semantic work DAG and dependency state;
- legal tool surface for the current task;
- action/effect accounting;
- bounded failure, repair, retry and reconciliation;
- trusted evidence and authoritative semantic completion;
- ordered progress events.

Keeping the boundary explicit avoids a second agent runtime.

## Middleware hooks

`before_agent` creates or validates the durable WorkGraph run and stores only its ID in agent state.

`wrap_model_call` derives the current directive, exposes only legal domain/control tools, injects compact WorkGraph context, and accounts semantic model-decision calls.

`wrap_tool_call` authorizes the final tool call, atomically journals intent, delegates actual execution through Deep Agents, records the receipt, and updates semantic state.

Both sync and async hook variants are implemented.

## HITL ordering

Deep Agents' HITL middleware pauses after the model proposes a tool call and before the tool node executes. This is the desired boundary:

- approve -> the original call reaches WorkGraph;
- edit -> edited arguments reach WorkGraph and undergo normal authorization/journaling;
- reject -> no domain execution reaches WorkGraph.

Do not add a second approval state machine around the same Deep Agents tool.

`create_work_graph_agent()` automatically derives `interrupt_on` entries from `ToolPolicy.requires_approval`. A checkpointer is still required by Deep Agents for HITL.

## Control tools

The model sees control tools only when semantically appropriate:

- `work_graph_status`
- `work_graph_expand`
- `work_graph_submit_result`
- `work_graph_repair`
- `work_graph_retry`
- `work_graph_reconcile`

During reconciliation, read-only tools marked with `ToolPolicy(read_only=True)` remain visible so an agent can inspect the external system before resolving an ambiguous effect.

## Composition

The live certification suite exercises WorkGraph inside realistic Deep Agents stacks rather than testing only direct engine calls. See `docs/CERTIFICATION.md` for the matrix.
