"""Unit tests for Step 2.6E reliability: timeouts, retries, failures and audit.

Mocks and injected failures only. Sleep, jitter and the clock are injected,
so nothing waits and results are deterministic. No AWS, Bedrock or network
calls; boto3/botocore are faked where a client is built.
"""

import importlib.util
import json
import logging
import sys
import types
import unittest
from unittest import mock

from northstar.integrations.synthetic_carrier import CarrierUnavailableError
from northstar.llm import bedrock_retry as br
from northstar.llm import bedrock_tool_loop as loop
from northstar.llm.bedrock_converse import BedrockInvocationError
from northstar.observability.audit import (
    FAILURE,
    SUCCESS,
    AuditEvent,
    Auditor,
    InMemoryAuditSink,
    LoggingAuditSink,
    safe_id,
)
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
)
from northstar.tools import carrier_tracking, registry

FULL = AccessContext(
    user_id="SYN-USER-0001",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
)
NO_CARRIER_SCOPE = AccessContext(
    user_id="SYN-USER-0002",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE}),
)
SECRET = "SECRET-DETAIL-do-not-log"


def bedrock_error(code, retryable=False):
    return BedrockInvocationError(f"Bedrock returned {code}: {SECRET}", code=code, retryable=retryable)


def tool_use(name, tool_input, tool_use_id):
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}


def response(content, stop_reason):
    return {
        "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200},
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    }


def asks(*blocks):
    return response(list(blocks), "tool_use")


def final(text=f"Summary: draft. {SECRET}"):
    return response([{"text": text}], "end_turn")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def policy(clock=None, **kwargs):
    clock = clock or FakeClock()
    kwargs.setdefault("random", lambda: 1.0)  # deterministic: top of the jitter range
    return br.RetryPolicy(sleep=clock.sleep, monotonic=clock.monotonic, **kwargs)


def scripted(*outcomes):
    """A ``_converse`` stand-in returning or raising each outcome in turn."""
    calls = []

    def fake(client, request):
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    fake.calls = calls
    return fake


def run(sink, *, converse, context=FULL, retry_policy=None, **kwargs):
    with mock.patch.object(br, "_converse", converse):
        return loop.run_tool_loop(
            "Why is the order delayed?",
            access_context=context,
            client=mock.Mock(),
            model_id="m",
            region="r",
            retry_policy=retry_policy or policy(),
            audit_sink=sink,
            correlation_id="corr-1",
            case_id="SYN-CASE-0001",
            **kwargs,
        )


def events_json(sink):
    return json.dumps([e.to_dict() for e in sink.events])


class RetryPolicyTests(unittest.TestCase):
    def test_defaults_follow_nfr_proposals(self):
        p = br.RetryPolicy()
        self.assertEqual(
            (p.max_attempts, p.base_delay_seconds, p.max_delay_seconds, p.total_budget_seconds),
            (3, 0.2, 2.0, 15.0),
        )
        self.assertEqual((p.connect_timeout_seconds, p.read_timeout_seconds), (5.0, 5.0))

    def test_invalid_values(self):
        for kwargs in (
            {"max_attempts": 0},
            {"max_attempts": True},
            {"max_attempts": 1.5},
            {"base_delay_seconds": 0},
            {"max_delay_seconds": -1},
            {"total_budget_seconds": "15"},
            {"read_timeout_seconds": 0},
            {"connect_timeout_seconds": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                br.RetryPolicy(**kwargs)

    def test_full_jitter_exponential_backoff_with_cap(self):
        top = br.RetryPolicy(random=lambda: 1.0)
        self.assertEqual([top.backoff(n) for n in range(1, 6)], [0.2, 0.4, 0.8, 1.6, 2.0])
        half = br.RetryPolicy(random=lambda: 0.5)
        self.assertEqual(half.backoff(2), 0.2)
        self.assertEqual(br.RetryPolicy(random=lambda: 0.0).backoff(3), 0.0)


class TransientClassificationTests(unittest.TestCase):
    def test_transient_codes(self):
        for code in (
            "ThrottlingException",
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelNotReadyException",
            "ModelTimeoutException",
            "ReadTimeoutError",
            "ConnectTimeoutError",
            "EndpointConnectionError",
            "ConnectionClosedError",
        ):
            with self.subTest(code=code):
                self.assertTrue(br.is_transient(bedrock_error(code)))

    def test_non_transient(self):
        # ParamValidationError is a BotoCoreError that _converse marks retryable;
        # it is a client-side problem, so it is not retried here.
        for code in (
            "AccessDeniedException",
            "ValidationException",
            "ResourceNotFoundException",
            "UnrecognizedClientException",
            "NoCredentialsError",
            "ParamValidationError",
            "max_tokens",
            "guardrail_intervened",
            "AccessDenied",
            "UnsupportedTool",
            "InvalidToolInput",
            None,
        ):
            with self.subTest(code=code):
                self.assertFalse(br.is_transient(bedrock_error(code, retryable=True)))
        self.assertFalse(br.is_transient(RuntimeError("ThrottlingException")))
        self.assertFalse(br.is_transient(loop.ToolLoopError("x", code="AccessDenied")))


class ConverseWithRetryTests(unittest.TestCase):
    def call(self, converse, retry_policy, hook=None):
        with mock.patch.object(br, "_converse", converse):
            return br.converse_with_retry(mock.Mock(), {"modelId": "m"}, retry_policy, on_failure=hook)

    def test_transient_then_success(self):
        clock = FakeClock()
        converse = scripted(bedrock_error("ThrottlingException", True), {"ok": True})
        self.assertEqual(self.call(converse, policy(clock)), {"ok": True})
        self.assertEqual(len(converse.calls), 2)
        self.assertEqual(clock.sleeps, [0.2])

    def test_attempts_are_bounded(self):
        clock = FakeClock()
        converse = scripted(*[bedrock_error("ServiceUnavailableException", True)] * 5)
        hook = mock.Mock()
        with self.assertRaises(BedrockInvocationError) as ctx:
            self.call(converse, policy(clock), hook)
        self.assertEqual(ctx.exception.code, "ServiceUnavailableException")
        self.assertEqual(len(converse.calls), 3)
        self.assertEqual(clock.sleeps, [0.2, 0.4])
        self.assertEqual(
            [(c.args[0], c.args[2], c.args[3]) for c in hook.call_args_list],
            [(1, True, 0.2), (2, True, 0.4), (3, False, 0.0)],
        )

    def test_max_attempts_is_configurable(self):
        converse = scripted(*[bedrock_error("ReadTimeoutError", True)] * 5)
        with self.assertRaises(BedrockInvocationError):
            self.call(converse, policy(max_attempts=1))
        self.assertEqual(len(converse.calls), 1)

    def test_non_transient_is_not_retried(self):
        for code in ("AccessDeniedException", "ValidationException", "ParamValidationError", "max_tokens"):
            with self.subTest(code=code):
                clock = FakeClock()
                converse = scripted(bedrock_error(code, retryable=True), {"ok": True})
                with self.assertRaises(BedrockInvocationError):
                    self.call(converse, policy(clock))
                self.assertEqual((len(converse.calls), clock.sleeps), (1, []))

    def test_unexpected_exception_is_not_retried(self):
        converse = scripted(RuntimeError(SECRET), {"ok": True})
        with self.assertRaises(RuntimeError):
            self.call(converse, policy())
        self.assertEqual(len(converse.calls), 1)

    def test_total_budget_stops_retries(self):
        clock = FakeClock()
        clock.now = 0.0

        def slow_failure(client, request):
            clock.now += 14.9  # the attempt itself used most of the budget
            raise bedrock_error("ThrottlingException", True)

        with self.assertRaises(BedrockInvocationError), mock.patch.object(br, "_converse", slow_failure):
            br.converse_with_retry(mock.Mock(), {}, policy(clock))
        self.assertEqual(clock.sleeps, [])  # 14.9 + 0.2 would exceed 15 s


class ClientTimeoutTests(unittest.TestCase):
    def test_client_gets_timeouts_and_no_botocore_retries(self):
        boto3 = types.ModuleType("boto3")
        boto3.client = mock.Mock(return_value="client")
        botocore = types.ModuleType("botocore")
        botocore_config = types.ModuleType("botocore.config")
        botocore_config.Config = mock.Mock(return_value="config")
        modules = {"boto3": boto3, "botocore": botocore, "botocore.config": botocore_config}
        with mock.patch.dict(sys.modules, modules):
            client = br.create_client_with_timeouts(
                "r", br.RetryPolicy(connect_timeout_seconds=2, read_timeout_seconds=7)
            )
        self.assertEqual(client, "client")
        botocore_config.Config.assert_called_once_with(
            connect_timeout=2, read_timeout=7, retries={"total_max_attempts": 1, "mode": "standard"}
        )
        boto3.client.assert_called_once_with("bedrock-runtime", region_name="r", config="config")

    def test_loop_builds_client_from_its_policy(self):
        sink = InMemoryAuditSink()
        retry_policy = policy(read_timeout_seconds=3)
        with mock.patch.object(loop, "create_client_with_timeouts", return_value=mock.Mock()) as create, \
                mock.patch.object(br, "_converse", scripted(final())):
            loop.run_tool_loop("q", access_context=FULL, region="r", model_id="m",
                               retry_policy=retry_policy, audit_sink=sink)
        create.assert_called_once_with("r", retry_policy)

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "botocore not installed")
    def test_real_botocore_errors_map_to_transient(self):
        from botocore.exceptions import ClientError, ReadTimeoutError

        client = mock.Mock()
        client.converse.side_effect = [
            ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"),
            ReadTimeoutError(endpoint_url="https://example.invalid"),
            {"ok": True},
        ]
        clock = FakeClock()
        self.assertEqual(br.converse_with_retry(client, {}, policy(clock)), {"ok": True})
        self.assertEqual(client.converse.call_count, 3)


class LoopRetryTests(unittest.TestCase):
    def test_transient_failure_retries_model_call_not_tool(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")),
            bedrock_error("ThrottlingException", True),
            final(),
        )
        with mock.patch.object(registry, "run_named_tool", wraps=registry.run_named_tool) as runner:
            result = run(sink, converse=converse)
        self.assertEqual(runner.call_count, 1)  # tool ran once
        self.assertEqual(len(converse.calls), 3)
        self.assertEqual(converse.calls[1]["messages"], converse.calls[2]["messages"])  # same request resent
        self.assertEqual(result.rounds, 1)
        failed = [e for e in sink.events if e.event_type == "model.call_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(dict(failed[0].details), {
            "error_code": "ThrottlingException", "attempt": 1, "max_attempts": 3, "retryable": True,
            "will_retry": True, "backoff_seconds": 0.2, "round": 2, "model_id": "m",
        })
        self.assertEqual(sink.types()[-1], "tool_loop.completed")

    def test_exhausted_retries_fail_and_are_audited(self):
        sink = InMemoryAuditSink()
        converse = scripted(*[bedrock_error("ModelTimeoutException", True)] * 3)
        with self.assertRaises(BedrockInvocationError) as ctx:
            run(sink, converse=converse)
        self.assertEqual(ctx.exception.code, "ModelTimeoutException")
        self.assertEqual(sink.types(), ["model.call_failed"] * 3 + ["tool_loop.failed"])
        self.assertEqual(sink.events[-1].details["error_code"], "ModelTimeoutException")

    def test_no_retry_for_tool_failures(self):
        cases = {
            "authorization": (NO_CARRIER_SCOPE, tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t1")),
            "invalid arguments": (FULL, tool_use("get_order_details", {"order_id": "SYN-ORD-0001", "tenant_id": "SYN-TENANT-B"}, "t1")),
            "unsupported tool": (FULL, tool_use("issue_refund", {"order_id": "SYN-ORD-0001"}, "t1")),
        }
        for label, (context, block) in cases.items():
            with self.subTest(label):
                sink = InMemoryAuditSink()
                converse = scripted(asks(block), final(), final())
                clock = FakeClock()
                with mock.patch.object(registry, "run_named_tool", wraps=registry.run_named_tool) as runner:
                    try:
                        run(sink, converse=converse, context=context, retry_policy=policy(clock))
                    except loop.ToolLoopError:
                        pass
                self.assertLessEqual(runner.call_count, 1)
                self.assertLessEqual(len(converse.calls), 2)  # never re-asked for the same round
                self.assertEqual(clock.sleeps, [])
                self.assertNotIn("model.call_failed", sink.types())


class AdapterFailureTests(unittest.TestCase):
    def failing_adapter(self, exc):
        adapter = mock.Mock()
        adapter.get_tracking.side_effect = exc
        return adapter

    def test_unexpected_adapter_exceptions_are_unverified(self):
        for exc in (TimeoutError(SECRET), ConnectionError(SECRET), ValueError(SECRET), KeyError(SECRET)):
            with self.subTest(exc=type(exc).__name__):
                adapter = self.failing_adapter(exc)
                result = carrier_tracking.get_carrier_tracking("SYN-ORD-0001", access_context=FULL, adapter=adapter)
                status = result["current_delivery_status"]
                self.assertEqual(status["verification"], "UNVERIFIED")
                self.assertEqual(status["status"], "UNKNOWN")
                self.assertEqual(status["reason"], carrier_tracking.REASON_ADAPTER_ERROR)
                self.assertIsNone(status["carrier_estimated_delivery"])
                self.assertEqual(result["tracking_events"], [])
                self.assertNotIn(SECRET, json.dumps(result))
                adapter.get_tracking.assert_called_once()  # no retry

    def test_unavailable_carrier_stays_unverified(self):
        adapter = self.failing_adapter(CarrierUnavailableError(SECRET))
        result = carrier_tracking.get_carrier_tracking("SYN-ORD-0001", access_context=FULL, adapter=adapter)
        self.assertEqual(result["current_delivery_status"]["verification"], "UNVERIFIED")
        self.assertEqual(result["current_delivery_status"]["reason"], carrier_tracking.REASON_UNAVAILABLE)
        adapter.get_tracking.assert_called_once()

    def test_adapter_fault_through_the_loop(self):
        sink = InMemoryAuditSink()
        adapter = self.failing_adapter(OSError(SECRET))
        converse = scripted(asks(tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t1")), final())
        with mock.patch.object(carrier_tracking, "SyntheticCarrierAdapter", return_value=adapter):
            run(sink, converse=converse)
        tool_result = converse.calls[1]["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual(tool_result["content"][0]["json"]["current_delivery_status"]["verification"], "UNVERIFIED")
        self.assertEqual(sink.types(), ["tool.requested", "tool.succeeded", "source.unavailable", "tool_loop.completed"])
        self.assertEqual(sink.events[2].details["failure_class"], carrier_tracking.REASON_ADAPTER_ERROR)
        self.assertEqual(sink.events[2].details["status_marked"], "UNVERIFIED")
        self.assertNotIn(SECRET, json.dumps(converse.calls[1]) + events_json(sink))

    def test_synthetic_unavailable_scenario_is_audited(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0006"}, "t1")), final())
        run(sink, converse=converse)
        self.assertIn("source.unavailable", sink.types())
        self.assertEqual(sink.events[2].details["failure_class"], carrier_tracking.REASON_UNAVAILABLE)

    def test_stale_data_is_not_an_outage(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0005"}, "t1")), final())
        run(sink, converse=converse)
        self.assertNotIn("source.unavailable", sink.types())

    def test_unexpected_tool_exception_becomes_unknown(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1"),
                tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t2"),
            ),
            final(),
        )
        with mock.patch.object(registry, "run_named_tool", side_effect=[RuntimeError(SECRET), {}]) as runner:
            result = run(sink, converse=converse)
        self.assertEqual(runner.call_count, 2)  # later tools still run; the failed one is not retried
        first = converse.calls[1]["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual((first["toolUseId"], first["status"]), ("t1", "error"))
        self.assertEqual(first["content"], [{"json": {"error": loop._TOOL_FAILED_MESSAGE}}])
        self.assertEqual(result.tool_calls[0].error_code, "ToolFailed")
        failed = sink.events[1]
        self.assertEqual((failed.event_type, failed.details["failure_class"]), ("tool.failed", "RuntimeError"))
        self.assertNotIn(SECRET, json.dumps(converse.calls[1]) + events_json(sink))


class AuditEventTests(unittest.TestCase):
    def test_success_sequence_and_common_fields(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0004"}, "t1"),
                tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0004"}, "t2"),
            ),
            final(),
        )
        run(sink, converse=converse)
        self.assertEqual(
            sink.types(),
            ["tool.requested", "tool.succeeded", "tool.requested", "tool.succeeded", "tool_loop.completed"],
        )
        for event in sink.events:
            self.assertEqual((event.actor, event.tenant_id), ("SYN-USER-0001", "SYN-TENANT-A"))
            self.assertEqual((event.correlation_id, event.case_id), ("corr-1", "SYN-CASE-0001"))
            self.assertIn("T", event.timestamp)
        self.assertEqual(len({e.event_id for e in sink.events}), len(sink.events))
        self.assertEqual(dict(sink.events[0].details), {"tool_name": "get_order_details", "tool_use_id": "t1", "round": 1})
        self.assertEqual(
            dict(sink.events[-1].details),
            {"rounds": 1, "tool_call_count": 2, "model_id": "m", "request_id": "req-1"},
        )

    def test_denial(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use("get_carrier_tracking", {"order_id": "SYN-ORD-0001"}, "t1")))
        with self.assertRaises(loop.ToolLoopError):
            run(sink, converse=converse, context=NO_CARRIER_SCOPE)
        self.assertEqual(sink.types(), ["tool.requested", "authorization.denied", "tool_loop.failed"])
        denied = sink.events[1]
        self.assertEqual((denied.outcome, denied.details["check"]), (FAILURE, "service"))
        self.assertEqual(sink.events[2].details["error_code"], "AccessDenied")

    def test_rejections(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use("get_order_details", {"order_id": 5}, "t1")), final())
        run(sink, converse=converse)
        self.assertEqual(sink.types()[:2], ["tool.requested", "tool.rejected"])
        self.assertEqual(sink.events[1].details["error_code"], "InvalidToolInput")

        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use(f"refund_{SECRET}", {"order_id": "SYN-ORD-0001"}, "t1")))
        with self.assertRaises(loop.ToolLoopError):
            run(sink, converse=converse)
        self.assertEqual(sink.types(), ["tool.rejected", "tool_loop.failed"])
        self.assertIsNone(sink.events[0].details["tool_name"])
        self.assertNotIn(SECRET, events_json(sink))

    def test_not_found_keeps_tenant_isolation(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0003"}, "t1")), final())
        run(sink, converse=converse)
        failed = sink.events[1]
        self.assertEqual((failed.event_type, failed.details["error_code"]), ("tool.failed", "OrderNotFound"))
        self.assertEqual(failed.tenant_id, "SYN-TENANT-A")  # from the trusted context, never the model

    def test_events_hold_no_arguments_results_or_model_text(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(
                tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1"),
                tool_use("get_order_details", {"order_id": "SYN-ORD-0002", "tenant_id": "SYN-TENANT-B"}, "t2"),
            ),
            final(),
        )
        run(sink, converse=converse)
        text = events_json(sink)
        for forbidden in ("SYN-ORD-0001", "SYN-ORD-0002", "SYN-TENANT-B", "SHIPPED", "2026-11-05", SECRET, "Summary"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_untrusted_tool_use_id_is_masked(self):
        self.assertEqual(safe_id("tooluse_abc-1"), "tooluse_abc-1")
        for bad in ("has space", "x" * 200, "a\nb", 7, f"id {SECRET}"):
            with self.subTest(bad=bad):
                self.assertEqual(safe_id(bad), "<invalid>")
        self.assertIsNone(safe_id(None))

    def test_event_payload_is_allowlisted(self):
        auditor = Auditor(InMemoryAuditSink(), actor="u", tenant_id="t", correlation_id="c")
        with self.assertRaises(ValueError):
            auditor.emit("tool.requested", SUCCESS, tool_input={"order_id": "SYN-ORD-0001"})
        with self.assertRaises(ValueError):
            auditor.emit("tool.requested", SUCCESS, tool_name=["list"])
        with self.assertRaises(ValueError):
            auditor.emit("tool.requested", "maybe")
        event = auditor.emit("tool.requested", SUCCESS, tool_name="get_order_details")
        with self.assertRaises(TypeError):
            event.details["tool_name"] = "changed"

    def test_logging_sink_writes_json(self):
        event = AuditEvent("e1", "tool.requested", "2026-11-10T00:00:00+00:00", "c", "u", "t", SUCCESS)
        with self.assertLogs("northstar.audit", level=logging.INFO) as logs:
            LoggingAuditSink().emit(event)
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(record["event_type"], "tool.requested")
        self.assertEqual(record["details"], {})

    def test_sink_failure_fails_closed(self):
        sink = mock.Mock()
        sink.emit.side_effect = OSError("audit store down")
        converse = scripted(asks(tool_use("get_order_details", {"order_id": "SYN-ORD-0001"}, "t1")), final())
        with mock.patch.object(registry, "run_named_tool") as runner, self.assertRaises(OSError):
            run(sink, converse=converse)
        runner.assert_not_called()  # tool.requested could not be recorded, so nothing ran


class ReadOnlyTests(unittest.TestCase):
    def test_new_modules_expose_no_write_paths(self):
        from northstar.observability import audit

        for module in (br, audit):
            for word in ("refund", "reship", "cancel", "write", "approve", "mcp", "langgraph"):
                with self.subTest(module=module.__name__, word=word):
                    self.assertFalse([n for n in dir(module) if word in n.lower()])
        self.assertEqual(registry.TOOL_NAMES, {"get_order_details", "get_carrier_tracking"})


if __name__ == "__main__":
    unittest.main()
