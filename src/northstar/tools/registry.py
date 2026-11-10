"""Read-only tool registry (Step 2.6C).

Maps a model-requested tool name to its runner and rejects any other name.
This is a lookup, not an execution loop: it calls one tool once and returns.
It does not select an architecture or agent count; alternative A can call the
tool functions directly.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Callable, Mapping

from northstar.tools import carrier_tracking, order_details

TOOL_SPECS: tuple[dict[str, Any], ...] = (order_details.TOOL_SPEC, carrier_tracking.TOOL_SPEC)

_RUNNERS: Mapping[str, Callable[..., dict[str, Any]]] = MappingProxyType(
    {
        order_details.TOOL_NAME: order_details.run_tool,
        carrier_tracking.TOOL_NAME: carrier_tracking.run_tool,
    }
)


# Step 2.6D: argument validators, so a caller can check model arguments
# before executing anything. Both tools currently accept only ``order_id``.
_VALIDATORS: Mapping[str, Callable[[object], object]] = MappingProxyType(
    {
        order_details.TOOL_NAME: order_details.validate_tool_input,
        carrier_tracking.TOOL_NAME: order_details.validate_tool_input,
    }
)

TOOL_NAMES: frozenset[str] = frozenset(_RUNNERS)


class UnsupportedToolError(LookupError):
    """The requested tool name is not registered."""


def validate_named_tool_input(tool_name: object, tool_input: object) -> None:
    """Raise ``UnsupportedToolError`` or ``InvalidToolInputError``; execute nothing."""
    validator = _VALIDATORS.get(tool_name) if isinstance(tool_name, str) else None
    if validator is None:
        raise UnsupportedToolError("unsupported tool")
    validator(tool_input)


def run_named_tool(tool_name: object, tool_input: object, *, access_context: object) -> dict[str, Any]:
    runner = _RUNNERS.get(tool_name) if isinstance(tool_name, str) else None
    if runner is None:
        raise UnsupportedToolError("unsupported tool")
    return runner(tool_input, access_context=access_context)
