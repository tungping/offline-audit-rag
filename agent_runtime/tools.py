import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import AgentSession, Evidence, Workspace


TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ToolAccessError(ValueError):
    """Raised when a requested tool call violates registry policy."""


class ToolExecutionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolContext:
    session: AgentSession
    session_dir: Path
    cancel_checker: Callable[[], bool]
    services: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    summary: str
    data: dict[str, Any]
    evidence: tuple[Evidence, ...] = ()
    retryable: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    workspace: Workspace
    required_args: frozenset[str]
    optional_args: frozenset[str]
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]

    def __post_init__(self) -> None:
        if not TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("tool name must use workspace.tool format")
        overlap = self.required_args & self.optional_args
        if overlap:
            raise ValueError(f"tool arguments cannot be both required and optional: {overlap}")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._execution_lock = threading.Lock()

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolAccessError(f"tool is not registered: {name}") from exc

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        spec = self.get(name)
        if spec.workspace is not context.session.workspace:
            raise ToolAccessError(
                f"tool workspace {spec.workspace.value} does not match "
                f"session workspace {context.session.workspace.value}"
            )

        provided = set(arguments)
        missing = spec.required_args - provided
        if missing:
            raise ToolAccessError(
                f"missing required arguments for {name}: {sorted(missing)}"
            )
        unknown = provided - spec.required_args - spec.optional_args
        if unknown:
            raise ToolAccessError(
                f"unknown arguments for {name}: {sorted(unknown)}"
            )
        if context.cancel_checker():
            raise InterruptedError("tool execution cancelled")

        with self._execution_lock:
            if context.cancel_checker():
                raise InterruptedError("tool execution cancelled")
            result = spec.handler(dict(arguments), context)
        if not isinstance(result, ToolResult):
            raise TypeError(f"tool {name} returned an invalid result")
        return result
