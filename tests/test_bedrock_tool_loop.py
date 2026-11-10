"""Unit tests for the bounded Bedrock Converse tool loop (Step 2.6D).

Mocked Converse client only. Synthetic data. No AWS, Bedrock or network
calls, no credentials.
"""

import json
import unittest
from unittest import mock

from northstar.llm import bedrock_tool_loop as loop
from northstar.llm.bedrock_converse import INVESTIGATION_TOOL_NAME, BedrockInvocationError
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
)
from northstar.tools import registry

FULL = AccessContext(
    user_id="SYN-USER-0001",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
)
OMS_ONLY = AccessContext(
    user_id="SYN-USER-0002",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE}),
)
QUESTION = "Why is order SYN-ORD-0001 delayed?"


def tool_use(name, tool_input, tool_use_id):
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}


def response(content, stop_reason):
    return {
        "ResponseMetadata": {"RequestId": "test-request-id", "HTTPStatusCode": 200},
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
    }


def asks(*blocks):
    return response(list(blocks), "tool_use")


def final(text="Summary: draft for the CS agent."):
    return response([{"text": text}], "end_turn")


def client_with(*responses):
    client = mock.Mock()
    client.converse.side_effect = list(responses)
    return client


def run(client, context=FULL, **kwargs):
    return loop.run_tool_loop(QUESTION, access_context=context, client=client, model_id="m", region="r", **kwargs)


def tool_results(request):
    """toolResult blocks of the last user message in a Converse request."""
    last = request["messages"][-1]
    assert last["role"] == "user"
    return [block["toolResult"] for block in last["content"]]


def request(client, index):
    return client.converse.call_args_list[index].kwargs


class ToolConfigTests(unittest.TestCase):
    def test_advertises_only_the_two_read_only_tools(self):
        config = loop.build_tool_loop_config()
        names = [spec["toolSpec"]["name"] for spec in config["tools"]]
        self.assertEqual(names, ["get_order_details", "get_carrier_tracking"])
        self.assertNotIn(INVESTIGATION_TOOL_NAME, names)
        self.assertNotIn("toolChoice", config)  # the model may answer without tools
        json.dumps(config)

    def test_config_is_a_copy(self):
        config = loop.build_tool_loop_config()
        config["tools"][0]["toolSpec"]["name"] = "changed"
        self.assertEqual(registry.TOOL_SPECS[0]["toolSpec"]["name"], "get_order_details")

    def test_every_request_carries_tools_and_system_prompt(self):
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")), final())
        run(client)
        for index in range(2):
            req = request(client, index)
            self.assertEqual(req["toolConfig"], loop.build_tool_loop_config())
            self.assertEqual(req["system"], [{"text": loop.TOOL_LOOP_SYSTEM_PROMPT}])
            self.assertEqual(req["modelId"], "m")


class SuccessTests(unittest.TestCase):
    def test_answer_without_tools(self):
        client = client_with(final("Summary: insufficient evidence."))
        result = run(client)
        self.assertEqual(result.final.text, "Summary: insufficient evidence.")
        self.assertEqual(result.final.stop_reason, "end_turn")
        self.assertEqual((result.rounds, result.tool_calls), (0, ()))
        self.assertEqual(client.converse.call_count, 1)

    def test_single_tool_round(self):
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "tooluse_A")), final())
        result = run(client)

        [tool_result] = tool_results(request(client, 1))
        self.assertEqual(tool_result["toolUseId"], "tooluse_A")
        self.assertEqual(tool_result["status"], "success")
        self.assertEqual(tool_result["content"][0]["json"]["order_status"], "SHIPPED")
        self.assertNotIn("tenant_id", tool_result["content"][0]["json"])

        # The assistant toolUse turn is echoed back before the results.
        self.assertEqual(request(client, 1)["messages"][1]["content"][0]["toolUse"]["toolUseId"], "tooluse_A")
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.tool_calls, (loop.ToolCallRecord(1, "tooluse_A", "get_order_details", "success"),))
        self.assertEqual(result.usage, {"inputTokens": 200, "outputTokens": 40, "totalTokens": 240})
        self.assertEqual(result.final.request_id, "test-request-id")
        self.assertEqual([m["role"] for m in result.messages], ["user", "assistant", "user", "assistant"])

    def test_sequential_rounds(self):
        client = client_with(
            asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0004"}, "t1")),
            asks({"text": "Now tracking."}, tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0004"}, "t2")),
            final(),
        )
        result = run(client)
        self.assertEqual(client.converse.call_count, 3)
        self.assertEqual(tool_results(request(client, 1))[0]["toolUseId"], "t1")
        [tracking] = tool_results(request(client, 2))
        self.assertEqual(tracking["toolUseId"], "t2")
        status = tracking["content"][0]["json"]["current_delivery_status"]
        self.assertEqual(status["verification"], "VERIFIED")
        self.assertEqual([c.round for c in result.tool_calls], [1, 2])
        self.assertEqual(len(request(client, 2)["messages"]), 5)

    def test_multiple_tool_uses_in_one_response(self):
        client = client_with(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0005"}, "t1"),
                tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0005"}, "t2"),
            ),
            final(),
        )
        result = run(client)
        results = tool_results(request(client, 1))
        self.assertEqual([r["toolUseId"] for r in results], ["t1", "t2"])
        self.assertEqual([r["status"] for r in results], ["success", "success"])
        stale = results[1]["content"][0]["json"]["current_delivery_status"]
        self.assertEqual((stale["verification"], stale["reason"]), ("UNVERIFIED", "STALE_CARRIER_DATA"))
        self.assertEqual(result.rounds, 1)

    def test_requests_are_snapshots(self):
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")), final())
        run(client)
        self.assertEqual(len(request(client, 0)["messages"]), 1)


class InvalidArgumentTests(unittest.TestCase):
    def assert_rejected_without_execution(self, tool_input):
        client = client_with(asks(tool_use("get_order_details", tool_input, "t1")), final())
        with mock.patch.object(registry, "run_named_tool") as runner:
            result = run(client)
        runner.assert_not_called()
        [tool_result] = tool_results(request(client, 1))
        self.assertEqual(tool_result["toolUseId"], "t1")
        self.assertEqual(tool_result["status"], "error")
        self.assertEqual(tool_result["content"], [{"json": {"error": loop._INVALID_INPUT_MESSAGE}}])
        self.assertEqual(result.tool_calls[0].error_code, "InvalidToolInput")

    def test_invalid_arguments(self):
        for tool_input in ({}, {"order_id": 7}, {"order_id": "1; DROP"}, "SYN-ORD-0001", None, ["SYN-ORD-0001"]):
            with self.subTest(tool_input=tool_input):
                self.assert_rejected_without_execution(tool_input)

    def test_model_cannot_supply_access_fields(self):
        for extra in (
            {"tenant_id": "SYN-TENANT-B"},
            {"user_permissions": [OMS_ORDER_READ]},
            {"access_context": {"tenant_id": "SYN-TENANT-B"}},
            {"tracking_number": "SYN-TRK-0003"},
        ):
            with self.subTest(extra=extra):
                self.assert_rejected_without_execution({"order_id": "SYN-ORD-0003", **extra})

    def test_error_message_does_not_echo_input(self):
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SECRET-VALUE"}, "t1")), final())
        run(client)
        self.assertNotIn("SECRET-VALUE", json.dumps(tool_results(request(client, 1))))

    def test_other_tenant_order_is_not_found(self):
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0003"}, "t1")), final())
        result = run(client)
        [tool_result] = tool_results(request(client, 1))
        self.assertEqual(tool_result["status"], "error")
        self.assertEqual(tool_result["content"], [{"json": {"error": "order not found"}}])
        self.assertEqual(result.tool_calls[0].error_code, "OrderNotFound")


class AuthorizationTests(unittest.TestCase):
    def test_missing_or_untrusted_context_fails_before_any_call(self):
        for context in (None, {"tenant_id": "SYN-TENANT-A"}, "SYN-TENANT-A"):
            with self.subTest(context=context):
                client = client_with(final())
                with mock.patch.object(loop, "create_client_with_timeouts") as create, self.assertRaises(loop.ToolLoopError) as ctx:
                    loop.run_tool_loop(QUESTION, access_context=context, client=client)
                self.assertEqual(ctx.exception.code, "MissingAuthorization")
                client.converse.assert_not_called()
                create.assert_not_called()

    def test_denied_tool_stops_the_loop(self):
        client = client_with(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1"),
                tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t2"),
            ),
            final(),
        )
        with self.assertRaises(loop.ToolLoopError) as ctx:
            run(client, context=OMS_ONLY)
        self.assertEqual(ctx.exception.code, "AccessDenied")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(client.converse.call_count, 1)  # nothing sent back to the model
        self.assertEqual([c.status for c in ctx.exception.tool_calls], ["success", "error"])
        self.assertEqual(ctx.exception.tool_calls[1].error_code, "AccessDenied")

    def test_context_without_permissions_is_denied(self):
        empty = AccessContext(user_id="SYN-USER-0003", tenant_id="SYN-TENANT-A")
        client = client_with(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")))
        with self.assertRaises(loop.ToolLoopError) as ctx:
            run(client, context=empty)
        self.assertEqual(ctx.exception.code, "AccessDenied")

    def test_trusted_context_is_passed_to_every_tool(self):
        client = client_with(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1"),
                tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t2"),
            ),
            final(),
        )
        with mock.patch.object(registry, "run_named_tool", return_value={}) as runner:
            run(client)
        for call in runner.call_args_list:
            self.assertIs(call.kwargs["access_context"], FULL)


class FailClosedTests(unittest.TestCase):
    def assert_loop_error(self, code, *responses, **kwargs):
        client = client_with(*responses)
        with mock.patch.object(registry, "run_named_tool") as runner, self.assertRaises(loop.ToolLoopError) as ctx:
            run(client, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return runner, ctx.exception

    def test_unsupported_tool_names(self):
        for name in (INVESTIGATION_TOOL_NAME, "issue_refund", "GET_ORDER_DETAILS", "", None, 3):
            with self.subTest(name=name):
                runner, _ = self.assert_loop_error(
                    "UnsupportedTool", asks(tool_use(name, {"order_id": "SYN-ORD-0001"}, "t1"))
                )
                runner.assert_not_called()

    def test_unsupported_name_in_later_block_runs_nothing(self):
        runner, _ = self.assert_loop_error(
            "UnsupportedTool",
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1"),
                tool_use("reship_order", {"order_id": "SYN-ORD-0001"}, "t2"),
            ),
        )
        runner.assert_not_called()

    def test_malformed_responses(self):
        good = tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")
        cases = {
            "not a dict": "oops",
            "no output": {"stopReason": "tool_use"},
            "wrong role": {"output": {"message": {"role": "user", "content": []}}, "stopReason": "end_turn"},
            "content not list": {"output": {"message": {"role": "assistant", "content": "x"}}, "stopReason": "end_turn"},
            "block not dict": response(["text"], "end_turn"),
            "toolUse not dict": asks({"toolUse": "x"}),
            "no toolUseId": asks({"toolUse": {"name": "get_order_details", "input": {"order_id": "SYN-ORD-0001"}}}),
            "blank toolUseId": asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, " ")),
            "duplicate toolUseId": asks(good, good),
            "tool_use without toolUse": asks({"text": "thinking"}),
            "end_turn with toolUse": response([good], "end_turn"),
            "no stopReason": {"output": {"message": {"role": "assistant", "content": [good]}}},
        }
        for label, bad in cases.items():
            with self.subTest(label):
                runner, _ = self.assert_loop_error("MalformedResponse", bad)
                runner.assert_not_called()

    def test_empty_final_text(self):
        self.assert_loop_error("EmptyResponse", response([{"text": "  "}], "end_turn"))

    def test_unexpected_stop_reasons(self):
        for stop_reason in ("stop_sequence", "unknown", "pause_turn"):
            with self.subTest(stop_reason=stop_reason):
                self.assert_loop_error("UnexpectedStopReason", response([{"text": "x"}], stop_reason))

    def test_blocked_or_truncated_output(self):
        for stop_reason in ("max_tokens", "guardrail_intervened", "content_filtered"):
            with self.subTest(stop_reason=stop_reason):
                client = client_with(response([{"text": "partial"}], stop_reason))
                with self.assertRaises(BedrockInvocationError) as ctx:
                    run(client)
                self.assertEqual(ctx.exception.code, stop_reason)


class LimitTests(unittest.TestCase):
    def endless(self):
        return [asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, f"t{i}")) for i in range(10)]

    def test_round_limit(self):
        client = client_with(*self.endless())
        with self.assertRaises(loop.ToolLoopError) as ctx:
            run(client, max_tool_rounds=2)
        self.assertEqual(ctx.exception.code, "ToolRoundLimitExceeded")
        self.assertEqual(client.converse.call_count, 3)  # third request is refused, not executed
        self.assertEqual(len(ctx.exception.tool_calls), 2)

    def test_default_round_limit_is_bounded(self):
        client = client_with(*self.endless())
        with self.assertRaises(loop.ToolLoopError):
            run(client)
        self.assertEqual(client.converse.call_count, loop.DEFAULT_MAX_TOOL_ROUNDS + 1)

    def test_answer_at_the_limit_succeeds(self):
        client = client_with(*self.endless()[:2], final())
        self.assertEqual(run(client, max_tool_rounds=2).rounds, 2)

    def test_tool_uses_per_round_limit(self):
        blocks = [tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, f"t{i}") for i in range(3)]
        client = client_with(asks(*blocks))
        with mock.patch.object(registry, "run_named_tool") as runner, self.assertRaises(loop.ToolLoopError) as ctx:
            run(client, max_tool_uses_per_round=2)
        self.assertEqual(ctx.exception.code, "TooManyToolUses")
        runner.assert_not_called()

    def test_invalid_limits(self):
        for value in (0, -1, 1.5, True, "2", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run(client_with(final()), max_tool_rounds=value)
                with self.assertRaises(ValueError):
                    run(client_with(final()), max_tool_uses_per_round=value)

    def test_empty_question(self):
        for question in ("", "   ", None):
            with self.subTest(question=question), self.assertRaises(ValueError):
                loop.run_tool_loop(question, access_context=FULL, client=client_with(final()))


class ReadOnlyTests(unittest.TestCase):
    def test_no_write_or_execution_functions_exposed(self):
        for word in ("refund", "reship", "cancel", "update", "write", "approve", "mcp", "langgraph"):
            for module in (loop, registry):
                with self.subTest(module=module.__name__, word=word):
                    self.assertFalse([n for n in dir(module) if word in n.lower()])

    def test_registry_names_match_specs(self):
        names = {spec["toolSpec"]["name"] for spec in registry.TOOL_SPECS}
        self.assertEqual(names, registry.TOOL_NAMES)
        self.assertEqual(names, {"get_order_details", "get_carrier_tracking"})

    def test_validate_named_tool_input_runs_nothing(self):
        with mock.patch.object(registry, "_RUNNERS", {}):
            registry.validate_named_tool_input("get_order_details", {"order_id": "SYN-ORD-0001"})
        with self.assertRaises(registry.UnsupportedToolError):
            registry.validate_named_tool_input("issue_refund", {"order_id": "SYN-ORD-0001"})


if __name__ == "__main__":
    unittest.main()
