"""``get_order_details``: read-only synthetic OMS order lookup (Step 2.6B).

[Proposal] The Converse ``toolSpec`` shape follows ADR-001 (Proposed). The
model may send only ``order_id``; unknown fields such as a tenant or customer
ID are rejected. Authorization comes from the separate ``AccessContext`` and
is checked before input validation or retrieval (fail closed).

Returns order_id, order_status, promised_delivery_date and carrier only.
No refunds, reships, order changes or other actions.
"""

from __future__ import annotations

import re
from typing import Any

from northstar.integrations.synthetic_oms import find_order
from northstar.security.access_context import OMS_ORDER_READ, OMS_READ_SCOPE, require_read

TOOL_NAME = "get_order_details"
ORDER_ID_PATTERN = r"^SYN-ORD-[0-9]{4}$"
_ORDER_ID_RE = re.compile(ORDER_ID_PATTERN)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "Order ID from the case or entered by the CS agent, e.g. SYN-ORD-0001.",
            "pattern": ORDER_ID_PATTERN,
        }
    },
    "required": ["order_id"],
    "additionalProperties": False,
}

TOOL_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": (
            "Read-only lookup of one order in the order management system. Returns "
            "order status, promised delivery date and carrier. Performs no actions."
        ),
        "inputSchema": {"json": INPUT_SCHEMA},
    }
}


class InvalidToolInputError(ValueError):
    """The model-supplied arguments do not match the input schema."""


class OrderNotFoundError(LookupError):
    """No such order is visible in the requesting user's tenant."""


def validate_tool_input(tool_input: object) -> str:
    """Return the validated ``order_id`` or raise ``InvalidToolInputError``."""
    if not isinstance(tool_input, dict):
        raise InvalidToolInputError("tool input must be an object")
    extra = set(tool_input) - {"order_id"}
    if extra:
        raise InvalidToolInputError("unexpected fields are not allowed")
    if "order_id" not in tool_input:
        raise InvalidToolInputError("order_id is required")
    order_id = tool_input["order_id"]
    if not isinstance(order_id, str):
        raise InvalidToolInputError("order_id must be a string")
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise InvalidToolInputError("order_id has an invalid format")
    return order_id


def get_order_details(order_id: object, *, access_context: object) -> dict[str, str]:
    """Return the four order fields for an authorized, known order."""
    context = require_read(access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
    validated_id = validate_tool_input({"order_id": order_id})
    record = find_order(validated_id, context.tenant_id)
    if record is None:
        raise OrderNotFoundError("order not found")
    return {
        "order_id": record.order_id,
        "order_status": record.order_status,
        "promised_delivery_date": record.promised_delivery_date,
        "carrier": record.carrier,
    }


def run_tool(tool_input: object, *, access_context: object) -> dict[str, str]:
    """Entry point for a future tool loop: model arguments and context stay separate."""
    require_read(access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
    order_id = validate_tool_input(tool_input)
    return get_order_details(order_id, access_context=access_context)
