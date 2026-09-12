from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkGraphError(Exception):
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class RevisionConflict(WorkGraphError):
    def __init__(self, message: str = "State revision changed") -> None:
        super().__init__("WG.STORE.REVISION_CONFLICT", message, retryable=True)


class GraphValidationError(WorkGraphError):
    pass


class ActionRejected(WorkGraphError):
    pass


class UnknownEffectError(WorkGraphError):
    """A tool reports that an external effect may have happened but is not known."""

    def __init__(self, message: str = "External effect outcome is unknown") -> None:
        super().__init__("WG.ACTION.UNKNOWN_EFFECT", message, retryable=False)
