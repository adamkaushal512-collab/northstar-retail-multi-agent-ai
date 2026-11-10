"""Unit tests for the read-only get_order_details tool (Step 2.6B).

Synthetic data only. No AWS, Bedrock or network calls.
"""

import json
import unittest
from unittest import mock

from northstar.security.access_context import (
    DENIED_MESSAGE,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
)
from northstar.tools import order_details
from northstar.tools.order_details import (
    TOOL_SPEC,
    InvalidToolInputError,
    OrderNotFoundError,
    get_order_details,
    run_tool,
)

TENANT_A = AccessContext(
    user_id="SYN-USER-0001",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE}),
)


class ToolSpecTests(unittest.TestCase):
    def test_spec_shape(self):
        spec = TOOL_SPEC["toolSpec"]
        self.assertEqual(spec["name"], "get_order_details")
        schema = spec["inputSchema"]["json"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["order_id"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(list(schema["properties"]), ["order_id"])
        self.assertEqual(schema["properties"]["order_id"]["type"], "string")
        json.dumps(TOOL_SPEC)  # serializable for the Converse request

    def test_schema_exposes_no_access_fields(self):
        text = json.dumps(TOOL_SPEC).lower()
        for word in ("tenant", "customer_id", "user_id", "permission"):
            self.assertNotIn(word, text)


class ValidInputTests(unittest.TestCase):
    def test_returns_exactly_the_four_fields(self):
        result = get_order_details("SYN-ORD-0001", access_context=TENANT_A)
        self.assertEqual(
            result,
            {
                "order_id": "SYN-ORD-0001",
                "order_status": "SHIPPED",
                "promised_delivery_date": "2026-11-05",
                "carrier": "SYN-CARRIER-1",
            },
        )

    def test_run_tool_with_model_arguments(self):
        result = run_tool({"order_id": "SYN-ORD-0002"}, access_context=TENANT_A)
        self.assertEqual(result["order_status"], "PROCESSING")
        self.assertNotIn("customer_id", result)
        self.assertNotIn("tenant_id", result)


class InvalidInputTests(unittest.TestCase):
    def test_missing_order_id(self):
        for tool_input in ({}, {"orderId": "SYN-ORD-0001"}):
            with self.subTest(tool_input=tool_input), self.assertRaises(InvalidToolInputError):
                run_tool(tool_input, access_context=TENANT_A)

    def test_invalid_types(self):
        for value in (None, 1, 1.5, True, ["SYN-ORD-0001"], {"id": "SYN-ORD-0001"}, b"SYN-ORD-0001"):
            with self.subTest(value=value), self.assertRaises(InvalidToolInputError):
                run_tool({"order_id": value}, access_context=TENANT_A)
            with self.subTest(direct=value), self.assertRaises(InvalidToolInputError):
                get_order_details(value, access_context=TENANT_A)

    def test_non_object_input(self):
        for tool_input in (None, "SYN-ORD-0001", ["SYN-ORD-0001"]):
            with self.subTest(tool_input=tool_input), self.assertRaises(InvalidToolInputError):
                run_tool(tool_input, access_context=TENANT_A)

    def test_bad_format(self):
        for value in ("", " ", "SYN-ORD-1", "syn-ord-0001", "SYN-ORD-0001 ", "SYN-ORD-0001\n", "x" * 1000):
            with self.subTest(value=value), self.assertRaises(InvalidToolInputError):
                run_tool({"order_id": value}, access_context=TENANT_A)

    def test_model_cannot_supply_access_fields(self):
        for extra in ({"tenant_id": "SYN-TENANT-B"}, {"customer_id": "SYN-CUST-0101"}, {"user_id": "x"}):
            tool_input = {"order_id": "SYN-ORD-0003", **extra}
            with self.subTest(extra=extra), self.assertRaises(InvalidToolInputError):
                run_tool(tool_input, access_context=TENANT_A)


class UnknownOrderTests(unittest.TestCase):
    def test_unknown_order(self):
        with self.assertRaises(OrderNotFoundError):
            get_order_details("SYN-ORD-9999", access_context=TENANT_A)

    def test_other_tenant_order_looks_unknown(self):
        """SYN-ORD-0003 exists in tenant B; tenant A gets the same error as unknown."""
        with self.assertRaises(OrderNotFoundError) as other:
            get_order_details("SYN-ORD-0003", access_context=TENANT_A)
        with self.assertRaises(OrderNotFoundError) as unknown:
            get_order_details("SYN-ORD-9999", access_context=TENANT_A)
        self.assertEqual(str(other.exception), str(unknown.exception))


class UnauthorizedAccessTests(unittest.TestCase):
    def _assert_denied(self, context, check):
        with mock.patch.object(order_details, "find_order") as find:
            with self.assertRaises(AccessDeniedError) as ctx:
                run_tool({"order_id": "SYN-ORD-0001"}, access_context=context)
            find.assert_not_called()  # denied before any retrieval
        self.assertEqual(ctx.exception.check, check)
        self.assertEqual(str(ctx.exception), DENIED_MESSAGE)
        self.assertNotIn("SHIPPED", str(ctx.exception))

    def test_no_context(self):
        self._assert_denied(None, "context")

    def test_wrong_context_type(self):
        self._assert_denied({"user_id": "SYN-USER-0001", "tenant_id": "SYN-TENANT-A"}, "context")

    def test_blank_identity(self):
        for user_id, tenant_id in (("", "SYN-TENANT-A"), ("SYN-USER-0001", " "), (None, "SYN-TENANT-A")):
            context = AccessContext(user_id, tenant_id, TENANT_A.user_permissions, TENANT_A.service_scopes)
            with self.subTest(user_id=user_id, tenant_id=tenant_id):
                self._assert_denied(context, "context")

    def test_mutable_permission_sets_rejected(self):
        context = AccessContext("SYN-USER-0001", "SYN-TENANT-A", {OMS_ORDER_READ}, frozenset({OMS_READ_SCOPE}))
        self._assert_denied(context, "context")

    def test_missing_user_permission(self):
        context = AccessContext("SYN-USER-0001", "SYN-TENANT-A", frozenset(), frozenset({OMS_READ_SCOPE}))
        self._assert_denied(context, "user")

    def test_missing_service_scope(self):
        context = AccessContext("SYN-USER-0001", "SYN-TENANT-A", frozenset({OMS_ORDER_READ}), frozenset())
        self._assert_denied(context, "service")

    def test_denied_before_input_validation(self):
        """An unauthenticated caller learns nothing, not even input validity."""
        with self.assertRaises(AccessDeniedError):
            run_tool({"order_id": 123}, access_context=None)


class ReadOnlyTests(unittest.TestCase):
    def test_returned_dict_is_a_copy(self):
        first = get_order_details("SYN-ORD-0001", access_context=TENANT_A)
        first["order_status"] = "CANCELLED"
        again = get_order_details("SYN-ORD-0001", access_context=TENANT_A)
        self.assertEqual(again["order_status"], "SHIPPED")

    def test_no_write_functions_exposed(self):
        public = {n for n in dir(order_details) if not n.startswith("_")}
        for word in ("refund", "reship", "cancel", "update", "create", "delete"):
            self.assertFalse([n for n in public if word in n.lower()], word)


if __name__ == "__main__":
    unittest.main()
