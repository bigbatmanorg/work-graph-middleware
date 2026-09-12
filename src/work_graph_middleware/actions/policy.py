from __future__ import annotations

from typing import Protocol

from work_graph_middleware.actions.models import ActionCandidate
from work_graph_middleware.core.models import RunState, Task


class ActionPolicy(Protocol):
    def allow(self, state: RunState, task: Task, candidate: ActionCandidate) -> bool: ...


class AllowAllPolicy:
    def allow(self, state: RunState, task: Task, candidate: ActionCandidate) -> bool:
        return True
