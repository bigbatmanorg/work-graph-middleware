from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from work_graph_middleware.actions.models import ActionCandidate, ActionDescriptor
from work_graph_middleware.core.models import Task

_WORD = re.compile(r"[a-zA-Z0-9_]+")


class ActionResolver(Protocol):
    def resolve(
        self,
        task: Task,
        tools: Sequence[ActionDescriptor],
        *,
        limit: int,
        expose_all_below: int,
    ) -> tuple[ActionCandidate, ...]: ...


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _WORD.finditer(text)}


class SimpleActionResolver:
    """Zero-dependency resolver with a conservative no-retrieval threshold.

    Small tool sets are exposed in full so a weak lexical match cannot hide the
    correct tool. Large sets are ranked deterministically by lexical overlap.
    """

    def resolve(
        self,
        task: Task,
        tools: Sequence[ActionDescriptor],
        *,
        limit: int,
        expose_all_below: int = 12,
    ) -> tuple[ActionCandidate, ...]:
        if not tools:
            return ()
        task_tokens = _tokens(f"{task.title} {task.description}")
        scored: list[tuple[float, str, ActionDescriptor]] = []
        for descriptor in tools:
            tool_tokens = _tokens(f"{descriptor.name} {descriptor.description}")
            union = task_tokens | tool_tokens
            score = len(task_tokens & tool_tokens) / len(union) if union else 0.0
            scored.append((score, descriptor.name, descriptor))

        if len(scored) <= expose_all_below:
            selected = sorted(scored, key=lambda item: item[1])
        else:
            selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]

        return tuple(
            ActionCandidate(id=f"A{index}", descriptor=descriptor, score=score)
            for index, (score, _name, descriptor) in enumerate(selected, start=1)
        )
