"""End-to-end failure scenario tests (Step 2.6F).

Each test drives a complete existing path: ``run_tool_loop`` with mocked
Converse responses, through the real registry, tools, synthetic OMS and
synthetic carrier adapter, retry module and auditor. Only ``_converse`` (the
Bedrock call), and where a scenario needs it the carrier adapter, OMS lookup
or audit sink, are replaced. No AWS, Bedrock, network, LangGraph or MCP.

Unit behavior already covered in ``test_bedrock_tool_loop``,
``test_reliability``, ``test_carrier_tracking_tool`` and
``test_bedrock_structured_output`` is not repeated; these tests check the
assembled outcome of a whole scenario.

Gap between the two Bedrock paths [Open]: ``run_tool_loop`` returns free text
only. It is never schema-validated here. ``investigate_delay_structured`` is
a separate call that takes evidence text and a platform-supplied
``carrier_status_verified`` flag; no production code connects the two. Where
a scenario validates structured output, the test itself composes them
(``structured_from_loop``): it takes the platform-produced ``toolResult``
payloads from the loop conversation as evidence and derives the flag from
the carrier tool's ``verification`` field, never from model text. That
composition is test scaffolding, not orchestration, and does not select an
architecture (ADR-001 to ADR-005 remain Proposed).
"""

import copy
import json
import logging
import unittest
from unittest import mock

from test_bedrock_structured_output import VALID, response_with_content, tool_block
from test_reliability import (
    FULL,
    SECRET,
    FakeClock,
    asks,
    bedrock_error,
    events_json,
    final,
    policy,
    run,
    scripted,
    tool_use,
)

from northstar.evidence.investigation_schema import DelayedDeliveryInvestigation, DeliveryStatus
from northstar.integrations import synthetic_oms
from northstar.llm import bedrock_converse as bc
from northstar.llm import bedrock_tool_loop as loop
from northstar.llm.bedrock_converse import BedrockInvocationError
from northstar.observability.audit import InMemoryAuditSink
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
)
from northstar.tools import carrier_tracking, order_details, registry

ORDERS = "get_order_details"
TRACKING = "get_carrier_tracking"

TENANT_B = AccessContext(
    user_id="SYN-USER-0101",
    tenant_id="SYN-TENANT-B",
    user_permissions=frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
)
# Synthetic, clearly fake credential-shaped value; must never be logged.
FAKE_CREDENTIAL = "SYN-FAKE-SECRET-ACCESS-KEY-0000"

# Values that must never appear in audit output (details or logs). The
# trusted actor and tenant are common fields by design (WF section 11).
FORBIDDEN_IN_AUDIT = (
    "SYN-ORD-",  # order IDs (tool arguments and results)
    "SYN-TRK-",  # tracking numbers (tool results)
    "SYN-CUST-",
    "SYN-TENANT-B",  # model-supplied or other-tenant IDs
    "SHIPPED",
    "OUT_FOR_DELIVERY",
    "IN_TRANSIT",
    "Summary",  # model text
    SECRET,
    FAKE_CREDENTIAL,
)
WRITE_WORDS = ("refund", "reship", "cancel", "credit", "update", "approve")


def results_sent(converse, call_index):
    """toolResult blocks the loop sent to the model in Converse call ``call_index``."""
    last = converse.calls[call_index]["messages"][-1]
    assert last["role"] == "user"
    return [block["toolResult"] for block in last["content"]]


def payload(tool_result):
    return tool_result["content"][0]["json"]


def platform_tool_results(result):
    """(tool name, status, payload) for each toolResult in the finished conversation."""
    names = {call.tool_use_id: call.name for call in result.tool_calls}
    out = []
    for message in result.messages:
        if message["role"] != "user":
            continue
        for block in message["content"]:
            if "toolResult" in block:
                tr = block["toolResult"]
                out.append((names[tr["toolUseId"]], tr["status"], payload(tr)))
    return out


def platform_verified(result):
    """True only if a successful carrier result says VERIFIED (never model text)."""
    return any(
        name == TRACKING
        and status == "success"
        and data["current_delivery_status"]["verification"] == carrier_tracking.VERIFIED
        for name, status, data in platform_tool_results(result)
    )


def structured_from_loop(result, order_id, model_payload):
    """Test-only composition of the two existing paths (see module docstring)."""
    evidence = json.dumps([data for _, _, data in platform_tool_results(result)], sort_keys=True)
    client = mock.Mock()
    client.converse.return_value = response_with_content([tool_block(model_payload)])
    return bc.investigate_delay_structured(
        evidence,
        order_id=order_id,
        carrier_status_verified=platform_verified(result),
        client=client,
        model_id="m",
        region="r",
    )


def draft(order_id, status, evidence=None):
    data = copy.deepcopy(VALID)
    data.update(order_id=order_id, delivery_status=status)
    if evidence is not None:
        data["evidence"] = evidence
    return data


class E2EAssertions(unittest.TestCase):
    def setUp(self):
        self.oms_before = dict(synthetic_oms.SYNTHETIC_ORDERS)
        runner = mock.patch.object(registry, "run_named_tool", wraps=registry.run_named_tool)
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def tearDown(self):
        # Read-only: OMS records are unchanged after every scenario [Decision D11].
        self.assertEqual(dict(synthetic_oms.SYNTHETIC_ORDERS), self.oms_before)

    def executed(self):
        """Names of tools actually executed through the registry."""
        return [c.args[0] for c in self.runner.call_args_list]

    def assert_read_only(self, converse):
        for request in converse.calls:
            names = [s["toolSpec"]["name"] for s in request["toolConfig"]["tools"]]
            self.assertEqual(names, [ORDERS, TRACKING])
        for name in self.executed():
            self.assertIn(name, registry.TOOL_NAMES)
            self.assertFalse(any(word in name for word in WRITE_WORDS))

    def assert_audit_clean(self, sink, tenant="SYN-TENANT-A"):
        text = events_json(sink)
        for forbidden in FORBIDDEN_IN_AUDIT:
            if forbidden != tenant:
                self.assertNotIn(forbidden, text)
        for event in sink.events:
            self.assertEqual(event.tenant_id, tenant)  # trusted context, never the model
            for key, value in event.details.items():
                self.assertNotIsInstance(value, (dict, list))


class NormalInvestigationTests(E2EAssertions):
    """Scenario 1: fresh, authoritative OMS and carrier evidence."""

    def test_fresh_evidence_is_verified_and_structured_draft_accepted(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0004"}, "t-oms"),
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0004"}, "t-trk")),
            final(),
        )
        result = run(sink, converse=converse)

        oms, trk = results_sent(converse, 1)
        self.assertEqual((oms["toolUseId"], oms["status"]), ("t-oms", "success"))
        self.assertEqual(payload(oms)["order_status"], "SHIPPED")
        self.assertEqual(payload(oms)["carrier"], "SYN-CARRIER-2")
        status = payload(trk)["current_delivery_status"]
        self.assertEqual((status["verification"], status["status"], status["reason"]),
                         ("VERIFIED", "OUT_FOR_DELIVERY", None))
        self.assertEqual(payload(trk)["freshness"]["status"], "FRESH")
        self.assertEqual(payload(trk)["source"], "SYN-CARRIER-TRACKING-API")
        self.assertEqual(self.executed(), [ORDERS, TRACKING])
        self.assertEqual(sink.types(), ["tool.requested", "tool.succeeded"] * 2 + ["tool_loop.completed"])
        self.assertTrue(platform_verified(result))

        investigation = structured_from_loop(result, "SYN-ORD-0004", draft("SYN-ORD-0004", "VERIFIED"))
        self.assertIsInstance(investigation, DelayedDeliveryInvestigation)
        self.assertIs(investigation.delivery_status, DeliveryStatus.VERIFIED)
        self.assertIs(investigation.requires_human_review, True)
        self.assert_read_only(converse)
        self.assert_audit_clean(sink)

    def test_free_text_answer_is_returned_unvalidated(self):
        # The tool loop's final answer is free text: it is returned as-is and
        # is not schema-validated; no tool or action follows from its wording.
        sink = InMemoryAuditSink()
        text = "Summary: VERIFIED. Refund issued and reship approved."
        converse = scripted(asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0004"}, "t1")), final(text))
        result = run(sink, converse=converse)
        self.assertEqual(result.final.text, text)
        self.assertIsInstance(result.final.text, str)
        self.assertEqual(self.executed(), [TRACKING])
        self.assert_read_only(converse)


class CarrierOutageTests(E2EAssertions):
    """Scenario 2: carrier unavailable; no cached or historical substitution."""

    def check_unverified(self, trk_payload, reason):
        status = trk_payload["current_delivery_status"]
        self.assertEqual(status, {"verification": "UNVERIFIED", "status": "UNKNOWN",
                                  "reason": reason, "carrier_estimated_delivery": None})
        self.assertEqual(trk_payload["tracking_events"], [])  # nothing substituted
        self.assertIsNone(trk_payload["source"])
        self.assertIsNone(trk_payload["source_as_of"])
        self.assertEqual(trk_payload["freshness"]["status"], "UNKNOWN")

    def test_synthetic_outage(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0006"}, "t1"),
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0006"}, "t2")),
            final(),
        )
        result = run(sink, converse=converse)
        oms, trk = results_sent(converse, 1)
        self.assertEqual(payload(oms)["order_status"], "SHIPPED")  # OMS still authoritative
        self.check_unverified(payload(trk), carrier_tracking.REASON_UNAVAILABLE)
        self.assertEqual(sink.types(), ["tool.requested", "tool.succeeded", "tool.requested",
                                        "tool.succeeded", "source.unavailable", "tool_loop.completed"])
        self.assertEqual(sink.events[4].details["status_marked"], "UNVERIFIED")
        self.assertFalse(platform_verified(result))

        accepted = structured_from_loop(result, "SYN-ORD-0006", draft("SYN-ORD-0006", "UNVERIFIED"))
        self.assertIs(accepted.delivery_status, DeliveryStatus.UNVERIFIED)
        with self.assertRaises(bc.InvalidStructuredOutputError) as ctx:
            structured_from_loop(result, "SYN-ORD-0006", draft("SYN-ORD-0006", "VERIFIED"))
        self.assertEqual(ctx.exception.code, "UnsupportedVerifiedStatus")
        self.assert_audit_clean(sink)

    def test_previously_seen_tracking_is_not_reused_after_outage(self):
        # Round 1 gets fresh tracking; then the carrier goes down. Round 2 must
        # be UNVERIFIED, not a cached copy of round 1.
        sink = InMemoryAuditSink()
        real = carrier_tracking.SyntheticCarrierAdapter()
        adapter = mock.Mock()
        adapter.get_tracking.side_effect = [
            real.get_tracking("SYN-CARRIER-1", "SYN-TRK-0001"),
            carrier_tracking.CarrierUnavailableError(SECRET),
        ]
        converse = scripted(
            asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "r1")),
            asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "r2")),
            final(),
        )
        with mock.patch.object(carrier_tracking, "SyntheticCarrierAdapter", return_value=adapter):
            result = run(sink, converse=converse)
        [first] = results_sent(converse, 1)
        [second] = results_sent(converse, 2)
        self.assertEqual(payload(first)["current_delivery_status"]["verification"], "VERIFIED")
        self.check_unverified(payload(second), carrier_tracking.REASON_UNAVAILABLE)
        self.assertEqual(adapter.get_tracking.call_count, 2)
        self.assertEqual(result.rounds, 2)
        self.assertNotIn(SECRET, json.dumps(converse.calls[2]) + events_json(sink))
        # Note: platform_verified() is True here because round 1 was VERIFIED.
        # Deciding which of several carrier results is current is not defined
        # by any existing path [Open]; see the module docstring.


class StaleTrackingTests(E2EAssertions):
    """Scenario 3: stale tracking keeps history but no verified current status."""

    def test_stale_tracking(self):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0005"}, "t1")), final())
        result = run(sink, converse=converse)
        [trk] = results_sent(converse, 1)
        data = payload(trk)
        status = data["current_delivery_status"]
        self.assertEqual((status["verification"], status["status"], status["reason"]),
                         ("UNVERIFIED", "UNKNOWN", carrier_tracking.REASON_STALE))
        self.assertIsNone(status["carrier_estimated_delivery"])
        self.assertEqual(data["freshness"]["status"], "STALE")
        self.assertGreater(data["freshness"]["age_seconds"], data["freshness"]["limit_seconds"])
        self.assertEqual([e["event_code"] for e in data["tracking_events"]], ["PICKED_UP", "IN_TRANSIT"])
        for event in data["tracking_events"]:
            self.assertEqual(event["evidence_type"], "HISTORICAL_CARRIER_EVENT")
            self.assertTrue(event["event_time"])
        self.assertNotIn("source.unavailable", sink.types())  # stale is not an outage
        self.assertFalse(platform_verified(result))

        historical = [{"classification": "Historical", "fact": "Carrier reported IN_TRANSIT.",
                       "source": "get_carrier_tracking"},
                      {"classification": "Unknown", "fact": "Current status.", "source": "none"}]
        accepted = structured_from_loop(result, "SYN-ORD-0005", draft("SYN-ORD-0005", "UNVERIFIED", historical))
        self.assertEqual(accepted.evidence[0].classification.value, "Historical")
        self.assertIs(accepted.requires_human_review, True)
        with self.assertRaises(bc.InvalidStructuredOutputError) as ctx:
            structured_from_loop(result, "SYN-ORD-0005", draft("SYN-ORD-0005", "VERIFIED"))
        self.assertEqual(ctx.exception.code, "UnsupportedVerifiedStatus")
        self.assert_audit_clean(sink)


class TenantIsolationTests(E2EAssertions):
    """Scenario 4: another tenant's order looks exactly like an unknown order."""

    def outcome(self, order_id, tool_name, context=FULL):
        sink = InMemoryAuditSink()
        converse = scripted(asks(tool_use(tool_name, {"order_id": order_id}, "t1")), final())
        result = run(sink, converse=converse, context=context)
        [tr] = results_sent(converse, 1)
        audit = [(e.event_type, e.outcome, dict(e.details)) for e in sink.events]
        return tr, audit, result.tool_calls, sink

    def test_cross_tenant_equals_unknown_for_both_tools(self):
        for tool_name in (ORDERS, TRACKING):
            with self.subTest(tool=tool_name):
                other, other_audit, other_calls, sink = self.outcome("SYN-ORD-0003", tool_name)
                unknown, unknown_audit, unknown_calls, _ = self.outcome("SYN-ORD-9999", tool_name)
                self.assertEqual(other, unknown)
                self.assertEqual(other, {"toolUseId": "t1", "status": "error",
                                         "content": [{"json": {"error": "order not found"}}]})
                self.assertEqual(other_audit, unknown_audit)
                self.assertEqual(other_calls, unknown_calls)
                self.assert_audit_clean(sink)

    def test_owner_tenant_can_read_it(self):
        tr, _, _, sink = self.outcome("SYN-ORD-0003", TRACKING, context=TENANT_B)
        self.assertEqual(tr["status"], "success")
        self.assertEqual(payload(tr)["current_delivery_status"]["verification"], "VERIFIED")
        self.assert_audit_clean(sink, tenant="SYN-TENANT-B")

    def test_model_cannot_switch_tenant(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0003", "tenant_id": "SYN-TENANT-B"}, "t1")),
            final(),
        )
        run(sink, converse=converse)
        [tr] = results_sent(converse, 1)
        self.assertEqual(payload(tr), {"error": loop._INVALID_INPUT_MESSAGE})
        self.assertEqual(self.executed(), [])
        self.assert_audit_clean(sink)


class InvalidToolCallTests(E2EAssertions):
    """Scenario 5: invalid or unsupported tool calls run nothing."""

    def test_unsupported_names_fail_closed_before_any_tool(self):
        names = ("issue_refund", "reship_order", bc.INVESTIGATION_TOOL_NAME, "", None, 7,
                 ["get_order_details"], {"name": "get_order_details"})
        for name in names:
            with self.subTest(name=name):
                self.runner.reset_mock()
                sink = InMemoryAuditSink()
                converse = scripted(asks(
                    tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "ok"),
                    {"toolUse": {"toolUseId": "bad", "name": name, "input": {"order_id": "SYN-ORD-0001"}}},
                ))
                with self.assertRaises(loop.ToolLoopError) as ctx:
                    run(sink, converse=converse)
                self.assertEqual(ctx.exception.code, "UnsupportedTool")
                self.assertEqual(self.executed(), [])  # the valid block in the same response did not run
                self.assertEqual(len(converse.calls), 1)
                self.assertEqual(sink.types(), ["tool.rejected", "tool_loop.failed"])
                self.assertIsNone(sink.events[0].details["tool_name"])
                self.assert_audit_clean(sink)

    def test_invalid_arguments_are_rejected_and_conversation_continues(self):
        bad_inputs = (None, [], "SYN-ORD-0001", {}, {"order_id": 1}, {"order_id": "ORD-1"},
                      {"order_id": "SYN-ORD-0001", "user_id": "SYN-USER-0009"},
                      {"order_id": f"SYN-ORD-0001; {SECRET}"})
        blocks = [tool_use(ORDERS, bad, f"t{i}") for i, bad in enumerate(bad_inputs[:4])]
        blocks2 = [tool_use(TRACKING, bad, f"u{i}") for i, bad in enumerate(bad_inputs[4:])]
        sink = InMemoryAuditSink()
        converse = scripted(asks(*blocks), asks(*blocks2), final())
        result = run(sink, converse=converse)
        self.assertEqual(self.executed(), [])
        for index, expected_ids in ((1, ["t0", "t1", "t2", "t3"]), (2, ["u0", "u1", "u2", "u3"])):
            sent = results_sent(converse, index)
            self.assertEqual([tr["toolUseId"] for tr in sent], expected_ids)
            for tr in sent:
                self.assertEqual((tr["status"], payload(tr)), ("error", {"error": loop._INVALID_INPUT_MESSAGE}))
        self.assertEqual({c.error_code for c in result.tool_calls}, {"InvalidToolInput"})
        self.assertNotIn(SECRET, json.dumps(converse.calls[2]["messages"][-1]) + events_json(sink))
        self.assert_audit_clean(sink)

    def test_malformed_tool_use_blocks_fail_closed(self):
        cases = {
            "missing id": {"toolUse": {"name": ORDERS, "input": {"order_id": "SYN-ORD-0001"}}},
            "duplicate id": None,
            "toolUse not an object": {"toolUse": ["x"]},
        }
        for label, block in cases.items():
            with self.subTest(label):
                self.runner.reset_mock()
                sink = InMemoryAuditSink()
                if block is None:
                    resp = asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "dup"),
                                tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "dup"))
                else:
                    resp = asks(block)
                with self.assertRaises(loop.ToolLoopError) as ctx:
                    run(sink, converse=scripted(resp))
                self.assertEqual(ctx.exception.code, "MalformedResponse")
                self.assertEqual(self.executed(), [])
                self.assertEqual(sink.types(), ["tool_loop.failed"])


class BedrockThrottlingTests(E2EAssertions):
    """Scenario 6: bounded retries, no duplicate tool execution, audited."""

    def test_throttle_and_timeout_between_rounds(self):
        sink = InMemoryAuditSink()
        clock = FakeClock()
        converse = scripted(
            bedrock_error("ThrottlingException", True),
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "t1")),
            bedrock_error("ReadTimeoutError", True),
            bedrock_error("ModelTimeoutException", True),
            asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "t2")),
            final(),
        )
        result = run(sink, converse=converse, retry_policy=policy(clock))
        self.assertEqual(self.executed(), [ORDERS, TRACKING])  # each tool exactly once
        self.assertEqual(len(converse.calls), 6)
        self.assertEqual(converse.calls[2]["messages"], converse.calls[3]["messages"])
        self.assertEqual(converse.calls[3]["messages"], converse.calls[4]["messages"])
        self.assertEqual(clock.sleeps, [0.2, 0.2, 0.4])
        self.assertEqual(sink.types(), [
            "model.call_failed", "tool.requested", "tool.succeeded",
            "model.call_failed", "model.call_failed", "tool.requested", "tool.succeeded",
            "tool_loop.completed",
        ])
        failed = [e.details for e in sink.events if e.event_type == "model.call_failed"]
        self.assertEqual([(d["error_code"], d["attempt"], d["round"], d["will_retry"]) for d in failed], [
            ("ThrottlingException", 1, 1, True),
            ("ReadTimeoutError", 1, 2, True),
            ("ModelTimeoutException", 2, 2, True),
        ])
        self.assertEqual((result.rounds, len(result.tool_calls)), (2, 2))
        self.assert_audit_clean(sink)

    def test_exhausted_retries_after_tools_ran(self):
        sink = InMemoryAuditSink()
        clock = FakeClock()
        throttle = bedrock_error("ThrottlingException", True)
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "t1"),
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "t2")),
            throttle, throttle, throttle, final(),
        )
        with self.assertRaises(BedrockInvocationError) as ctx:
            run(sink, converse=converse, retry_policy=policy(clock))
        self.assertEqual(ctx.exception.code, "ThrottlingException")
        self.assertEqual(self.executed(), [ORDERS, TRACKING])
        self.assertEqual(len(converse.calls), 4)  # 1 + max_attempts; final() never reached
        self.assertEqual(sink.types(), ["tool.requested", "tool.succeeded"] * 2
                         + ["model.call_failed"] * 3 + ["tool_loop.failed"])
        self.assertEqual([e.details["will_retry"] for e in sink.events[4:7]], [True, True, False])
        self.assertEqual(dict(sink.events[-1].details),
                         {"error_code": "ThrottlingException", "tool_call_count": 2, "model_id": "m"})
        self.assert_audit_clean(sink)

    def test_non_transient_error_is_not_retried(self):
        sink = InMemoryAuditSink()
        clock = FakeClock()
        denied = BedrockInvocationError(f"Bedrock returned AccessDeniedException: {FAKE_CREDENTIAL}",
                                        code="AccessDeniedException")
        converse = scripted(asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "t1")), denied, final())
        with self.assertRaises(BedrockInvocationError):
            run(sink, converse=converse, retry_policy=policy(clock))
        self.assertEqual((len(converse.calls), clock.sleeps, self.executed()), (2, [], [ORDERS]))
        self.assertEqual(sink.types()[-2:], ["model.call_failed", "tool_loop.failed"])
        self.assert_audit_clean(sink)


class FailingSink(InMemoryAuditSink):
    """Records events, then raises on the ``fail_at``-th emit (0-based)."""

    def __init__(self, fail_at):
        super().__init__()
        self.fail_at = fail_at
        self.attempts = 0

    def emit(self, event):
        self.attempts += 1
        if self.attempts - 1 == self.fail_at:
            raise OSError("audit store unavailable")
        super().emit(event)


class AdapterAndAuditFailureTests(E2EAssertions):
    """Scenario 7: unexpected adapter and audit sink failures fail closed."""

    def test_oms_adapter_fault_is_unknown_and_not_leaked(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "t1"),
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "t2")),
            final(),
        )
        fault = ConnectionError(f"OMS down {SECRET} {FAKE_CREDENTIAL}")
        with mock.patch.object(order_details, "find_order", side_effect=fault), \
                mock.patch.object(carrier_tracking, "find_order", side_effect=fault):
            result = run(sink, converse=converse)
        sent = results_sent(converse, 1)
        for tr in sent:
            self.assertEqual((tr["status"], payload(tr)), ("error", {"error": loop._TOOL_FAILED_MESSAGE}))
        self.assertEqual([c.error_code for c in result.tool_calls], ["ToolFailed", "ToolFailed"])
        self.assertEqual(self.executed(), [ORDERS, TRACKING])  # each once, not retried
        self.assertEqual([e.details.get("failure_class") for e in sink.events if e.event_type == "tool.failed"],
                         ["ConnectionError", "ConnectionError"])
        self.assertFalse(platform_verified(result))
        leaked = json.dumps(converse.calls[1]) + events_json(sink) + result.final.text
        self.assertNotIn(FAKE_CREDENTIAL, leaked)
        self.assert_audit_clean(sink)

    def test_carrier_adapter_fault_is_unverified(self):
        sink = InMemoryAuditSink()
        adapter = mock.Mock()
        adapter.get_tracking.side_effect = MemoryError(FAKE_CREDENTIAL)
        converse = scripted(asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0004"}, "t1")), final())
        with mock.patch.object(carrier_tracking, "SyntheticCarrierAdapter", return_value=adapter):
            result = run(sink, converse=converse)
        [tr] = results_sent(converse, 1)
        self.assertEqual(payload(tr)["current_delivery_status"]["reason"], carrier_tracking.REASON_ADAPTER_ERROR)
        self.assertEqual(payload(tr)["tracking_events"], [])
        self.assertFalse(platform_verified(result))
        with self.assertRaises(bc.InvalidStructuredOutputError):
            structured_from_loop(result, "SYN-ORD-0004", draft("SYN-ORD-0004", "VERIFIED"))
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(converse.calls[1]) + events_json(sink))
        self.assert_audit_clean(sink)

    def test_audit_sink_failure_at_every_point_fails_closed(self):
        # Normal run emits: requested, succeeded, requested, succeeded, completed.
        expected_executed = {0: [], 1: [ORDERS], 2: [ORDERS], 3: [ORDERS, TRACKING], 4: [ORDERS, TRACKING]}
        for fail_at, executed in expected_executed.items():
            with self.subTest(fail_at=fail_at):
                self.runner.reset_mock()
                sink = FailingSink(fail_at)
                converse = scripted(
                    asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "t1"),
                         tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "t2")),
                    final(),
                )
                with self.assertRaises(OSError) as ctx:
                    run(sink, converse=converse)  # no result is returned
                self.assertEqual(str(ctx.exception), "audit store unavailable")
                self.assertEqual(self.executed(), executed)
                self.assertEqual(len(converse.calls), 1 if fail_at < 4 else 2)
                self.assertNotIn(SECRET, str(ctx.exception))
                self.assert_audit_clean(sink)

    def test_audit_sink_failure_during_retry_stops_retrying(self):
        sink = FailingSink(0)
        clock = FakeClock()
        converse = scripted(bedrock_error("ThrottlingException", True), final())
        with self.assertRaises(OSError):
            run(sink, converse=converse, retry_policy=policy(clock))
        self.assertEqual((len(converse.calls), clock.sleeps), (1, []))

    def test_default_logging_sink_output_is_clean(self):
        converse = scripted(
            asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0004", "tenant_id": "SYN-TENANT-B"}, "t0"),
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0006"}, "t1")),
            bedrock_error("ThrottlingException", True),
            asks(tool_use(f"refund_{SECRET}", {"order_id": "SYN-ORD-0004"}, "t2")),
        )
        with self.assertLogs("northstar.audit", level=logging.INFO) as logs, \
                self.assertRaises(loop.ToolLoopError):
            run(None, converse=converse)
        records = [json.loads(r.getMessage()) for r in logs.records]
        self.assertEqual([r["event_type"] for r in records], [
            "tool.requested", "tool.rejected", "tool.requested", "tool.succeeded", "source.unavailable",
            "model.call_failed", "tool.rejected", "tool_loop.failed",
        ])
        text = "\n".join(logs.output)
        for forbidden in FORBIDDEN_IN_AUDIT:
            self.assertNotIn(forbidden, text)


class MultiRoundTests(E2EAssertions):
    """Scenario 8: multi-tool and multi-round toolUseId matching and evidence."""

    def test_ids_and_evidence_match_across_rounds(self):
        sink = InMemoryAuditSink()
        converse = scripted(
            asks({"text": "Checking."},
                 tool_use(TRACKING, {"order_id": "SYN-ORD-0001"}, "tooluse_Z"),
                 tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, "tooluse_A"),
                 tool_use(ORDERS, {"order_id": "SYN-ORD-0003"}, "tooluse_M"),
                 tool_use(ORDERS, {"order_id": 42}, "tooluse_B")),
            asks(tool_use(TRACKING, {"order_id": "SYN-ORD-0005"}, "tooluse_Z2"),
                 tool_use(ORDERS, {"order_id": "SYN-ORD-0002"}, "tooluse_A2")),
            final(),
        )
        result = run(sink, converse=converse)

        r1 = results_sent(converse, 1)
        self.assertEqual([tr["toolUseId"] for tr in r1], ["tooluse_Z", "tooluse_A", "tooluse_M", "tooluse_B"])
        self.assertEqual([tr["status"] for tr in r1], ["success", "success", "error", "error"])
        self.assertEqual(payload(r1[0])["order_id"], "SYN-ORD-0001")
        self.assertEqual(payload(r1[0])["current_delivery_status"]["status"], "EXCEPTION")
        self.assertEqual(payload(r1[0])["carrier_exception_codes"], ["WEATHER_DELAY"])
        self.assertEqual(payload(r1[1])["promised_delivery_date"], "2026-11-05")
        self.assertEqual(payload(r1[2]), {"error": "order not found"})
        self.assertEqual(payload(r1[3]), {"error": loop._INVALID_INPUT_MESSAGE})

        r2 = results_sent(converse, 2)
        self.assertEqual([tr["toolUseId"] for tr in r2], ["tooluse_Z2", "tooluse_A2"])
        self.assertEqual(payload(r2[0])["current_delivery_status"]["reason"], carrier_tracking.REASON_STALE)
        self.assertEqual(payload(r2[1])["order_status"], "PROCESSING")
        # Round 2 request still carries round 1 exactly, in order.
        self.assertEqual(converse.calls[2]["messages"][:3], converse.calls[1]["messages"])

        roles = [m["role"] for m in result.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user", "assistant"])
        for assistant, user in ((result.messages[1], result.messages[2]), (result.messages[3], result.messages[4])):
            asked = [b["toolUse"]["toolUseId"] for b in assistant["content"] if "toolUse" in b]
            answered = [b["toolResult"]["toolUseId"] for b in user["content"]]
            self.assertEqual(asked, answered)
        self.assertEqual([(c.round, c.tool_use_id, c.status, c.error_code) for c in result.tool_calls], [
            (1, "tooluse_Z", "success", None), (1, "tooluse_A", "success", None),
            (1, "tooluse_M", "error", "OrderNotFound"), (1, "tooluse_B", "error", "InvalidToolInput"),
            (2, "tooluse_Z2", "success", None), (2, "tooluse_A2", "success", None),
        ])
        self.assertEqual(self.executed(), [TRACKING, ORDERS, ORDERS, TRACKING, ORDERS])
        self.assertEqual([(e.event_type, e.details.get("tool_use_id"), e.details.get("round")) for e in sink.events], [
            ("tool.requested", "tooluse_Z", 1), ("tool.succeeded", "tooluse_Z", 1),
            ("tool.requested", "tooluse_A", 1), ("tool.succeeded", "tooluse_A", 1),
            ("tool.requested", "tooluse_M", 1), ("tool.failed", "tooluse_M", 1),
            ("tool.requested", "tooluse_B", 1), ("tool.rejected", "tooluse_B", 1),
            ("tool.requested", "tooluse_Z2", 2), ("tool.succeeded", "tooluse_Z2", 2),
            ("tool.requested", "tooluse_A2", 2), ("tool.succeeded", "tooluse_A2", 2),
            ("tool_loop.completed", None, None),
        ])
        self.assertEqual(sink.events[-1].details["rounds"], 2)
        self.assertEqual(result.usage["totalTokens"], 45)
        self.assert_read_only(converse)
        self.assert_audit_clean(sink)

    def test_unsafe_tool_use_id_is_masked_in_audit_but_matched_for_model(self):
        sink = InMemoryAuditSink()
        unsafe = f"id with spaces {SECRET}"
        converse = scripted(asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, unsafe)), final())
        run(sink, converse=converse)
        [tr] = results_sent(converse, 1)
        self.assertEqual(tr["toolUseId"], unsafe)  # Converse needs the exact ID back
        self.assertEqual({e.details.get("tool_use_id") for e in sink.events[:2]}, {"<invalid>"})
        self.assert_audit_clean(sink)

    def test_round_limit_stops_before_extra_tools(self):
        sink = InMemoryAuditSink()
        rounds = [asks(tool_use(ORDERS, {"order_id": "SYN-ORD-0001"}, f"t{i}")) for i in range(3)]
        with self.assertRaises(loop.ToolLoopError) as ctx:
            run(sink, converse=scripted(*rounds), max_tool_rounds=2)
        self.assertEqual(ctx.exception.code, "ToolRoundLimitExceeded")
        self.assertEqual(self.executed(), [ORDERS, ORDERS])
        self.assertEqual(sink.types()[-1], "tool_loop.failed")
        self.assertEqual(sink.events[-1].details["tool_call_count"], 2)


if __name__ == "__main__":
    unittest.main()
