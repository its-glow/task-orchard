"""Domain errors.

Every failure the service can report is an OrchardError. The message says what
went wrong; the hint says what to do instead, written for a small model that
will read it and retry. The MCP layer turns these into tool errors verbatim
(SPEC section 4.2), so write both as if the agent is the reader.
"""

from __future__ import annotations

from typing import Any

from task_orchard.core.enums import ErrorCode


class OrchardError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code.value, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out


class NotFound(OrchardError):
    def __init__(self, what: str, ident: object) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            f"There is no {what} with id {ident}.",
            hint="Use search_tasks or get_tree to find the right id. Never guess an id.",
        )


class Invalid(OrchardError):
    def __init__(self, message: str, *, hint: str | None = None, **details: Any) -> None:
        super().__init__(ErrorCode.INVALID, message, hint=hint, details=details)


class CycleError(OrchardError):
    """The requested link or move would make tasks wait on each other."""

    def __init__(self, path: list[dict[str, Any]]) -> None:
        # `path` is the loop as [{id, title, status, because}], in
        # must-finish-first order; the last entry repeats the first. `because`
        # describes the edge to the next entry. Closed tasks can be on it.
        chain = " -> ".join(f"#{step['id']}" for step in path)
        super().__init__(
            ErrorCode.CYCLE,
            f"That would create a loop where tasks wait on each other: {chain}.",
            hint=(
                "Link or move different tasks, or first remove a 'blocks' link in the loop "
                "with unlink_tasks (it works on closed tasks too)."
            ),
            details={"path": path},
        )


class NotOpen(OrchardError):
    def __init__(self, task_id: int, status: str, *, doing: str = "change that") -> None:
        super().__init__(
            ErrorCode.NOT_OPEN,
            f"Task {task_id} is {status}, so you cannot {doing}.",
            hint=(
                "If the user wants to work on it again, call reopen_task first. "
                "On a closed task, update_task can still change description, notes "
                "and pattern_id."
            ),
            details={"task_id": task_id, "status": status},
        )


class ParentClosed(OrchardError):
    def __init__(self, parent_id: int, status: str) -> None:
        super().__init__(
            ErrorCode.PARENT_CLOSED,
            f"Task {parent_id} is {status}, so nothing can be added under it.",
            hint="Reopen the parent with reopen_task first, or choose an open parent.",
            details={"parent_id": parent_id, "status": status},
        )


class Unsupported(OrchardError):
    def __init__(self, message: str, *, hint: str, **details: Any) -> None:
        super().__init__(ErrorCode.UNSUPPORTED, message, hint=hint, details=details)
