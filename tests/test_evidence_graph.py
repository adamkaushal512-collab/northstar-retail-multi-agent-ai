"""Unit tests for the Step 3.3 sequential OMS -> carrier evidence graph.

Synthetic data and in-memory adapters only. No AWS, Bedrock, carrier or
other network calls. Skipped when the optional ``langgraph`` extra is not
installed (``pip install .[langgraph]``).
"""

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from northstar.evidence.investigation_schema import DeliveryStatus
from northstar.integrations.synthetic_carrier import CarrierTrackingResponse, SyntheticCarrierAdapter
from northstar.orchestration.multi_agent import state as st
from northstar.orchestration.multi_agent.state import (
    InvalidStateError,
    InvestigationRuntimeContext,
    effective_delivery_status,
    initial_state,
    is_trusted_carrier_evidence,
    make_safe_error,
    validate_state,
)
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
)

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
if HAS_LANGGRAPH:
    from northstar.orchestration.multi_agent import evidence_graph as eg

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SECRET_TEXT = "SYN-SECRET-should-never-appear"


def access(user_perms=(OMS_ORDER_READ, CARRIER_TRACKING_READ), scopes=(OMS_READ_SCOPE, CARRIER_READ_SCOPE)):
    return AccessContext("SYN-USER-0001", "SYN-TENANT-A", frozenset(user_perms), frozenset(scopes))


RUNTIME = None if not HAS_LANGGRAPH else InvestigationRuntimeContext(access_context=access())


def graph(**kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("carrier_adapter", SyntheticCarrierAdapter(clock=lambda: NOW))
    return eg.build_evidence_graph(**kwargs)


def run(order_id, context=RUNTIME, state=None, **kwargs):
    return graph(**kwargs).invoke(state or initial_state(order_id), context=context)


def codes(out):
    return [(e["node"], e["error_code"], e["source_system"]) for e in out["errors"]]


class _RaisingAdapter:
    def get_tracking(self, carrier, tracking_number):
        raise RuntimeError(SECRET_TEXT)


class _WrongTrackingAdapter:
    """Returns a well-typed response for a different shipment (malformed)."""

    def get_tracking(self, carrier, tracking_number):
        return CarrierTrackingResponse(carrier, "SYN-TRK-9999", "SYN-SRC", NOW, (), (), None)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class GraphStructureTests(unittest.TestCase):
    def test_graph_is_linear_oms_then_carrier(self):
        drawn = graph().get_graph()
        self.assertEqual(set(drawn.nodes), {"__start__", "__end__", *eg.NODE_ORDER})
        edges = [(e.source, e.target, e.conditional) for e in drawn.edges]
        self.assertEqual(
            sorted(edges),
            sorted(
                [
                    ("__start__", eg.OMS_NODE, False),
                    (eg.OMS_NODE, eg.CARRIER_NODE, False),
                    (eg.CARRIER_NODE, "__end__", False),
                ]
            ),
        )

    def test_nodes_run_in_order_once_each(self):
        updates = graph().stream(initial_state("SYN-ORD-0004"), context=RUNTIME, stream_mode="updates")
        self.assertEqual([next(iter(u)) for u in updates], list(eg.NODE_ORDER))

    def test_no_checkpointer_is_attached(self):
        self.assertIsNone(graph().checkpointer)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class SuccessfulRetrievalTests(unittest.TestCase):
    def test_fresh_carrier_evidence_is_verified_and_trusted(self):
        out = run("SYN-ORD-0004")
        validate_state(out)
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)
        self.assertIs(effective_delivery_status(out), DeliveryStatus.VERIFIED)
        self.assertTrue(is_trusted_carrier_evidence(out["carrier_evidence"]))
        self.assertEqual(out["carrier_evidence"]["payload"]["current_delivery_status"]["status"], "OUT_FOR_DELIVERY")
        self.assertEqual(out["errors"], [])
        self.assertIs(out["requires_human_review"], True)
        self.assertIsNone(out["final_investigation"])

    def test_oms_evidence_is_retrieved_but_never_verifies(self):
        out = run("SYN-ORD-0004")
        oms = out["oms_evidence"]
        self.assertEqual(oms["source_system"], st.SOURCE_OMS)
        self.assertEqual(oms["retrieval_status"], st.RETRIEVED)
        self.assertIs(oms["verification"], DeliveryStatus.UNVERIFIED)
        self.assertIsNone(oms["attestation"])
        self.assertEqual(oms["retrieved_at"], NOW.isoformat())
        self.assertIsNone(oms["source"])  # the OMS tool names no source; none is invented
        self.assertEqual(
            set(oms["payload"]), {"order_id", "order_status", "promised_delivery_date", "carrier"}
        )


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class AuthorizationTests(unittest.TestCase):
    def assert_denied(self, out, nodes):
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(out["requires_human_review"], True)
        for node, slot, system in (
            (eg.OMS_NODE, "oms_evidence", st.SOURCE_OMS),
            (eg.CARRIER_NODE, "carrier_evidence", st.SOURCE_CARRIER),
        ):
            if node in nodes:
                self.assertEqual(out[slot]["retrieval_status"], st.DENIED)
                self.assertIsNone(out[slot]["payload"])
                self.assertIn((node, eg.ACCESS_DENIED, system), codes(out))
        validate_state(out)

    def test_missing_runtime_context_denies_both_retrievals(self):
        self.assert_denied(run("SYN-ORD-0004", context=None), eg.NODE_ORDER)

    def test_wrong_context_type_is_denied(self):
        # A bare AccessContext is not the trusted runtime context wrapper.
        self.assert_denied(run("SYN-ORD-0004", context=access()), eg.NODE_ORDER)

    def test_missing_carrier_permission_denies_only_carrier(self):
        ctx = InvestigationRuntimeContext(access(user_perms=(OMS_ORDER_READ,)))
        out = run("SYN-ORD-0004", context=ctx)
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.RETRIEVED)
        self.assert_denied(out, (eg.CARRIER_NODE,))
        self.assertEqual(codes(out), [(eg.CARRIER_NODE, eg.ACCESS_DENIED, st.SOURCE_CARRIER)])

    def test_missing_service_scope_is_denied(self):
        ctx = InvestigationRuntimeContext(access(scopes=(CARRIER_READ_SCOPE,)))
        self.assert_denied(run("SYN-ORD-0004", context=ctx), eg.NODE_ORDER)

    def test_other_tenant_order_is_not_found_without_data(self):
        out = run("SYN-ORD-0003")  # tenant B order, tenant A user
        for slot in ("oms_evidence", "carrier_evidence"):
            self.assertEqual(out[slot]["retrieval_status"], st.NOT_FOUND)
            self.assertIsNone(out[slot]["payload"])
        self.assertEqual(
            codes(out),
            [
                (eg.OMS_NODE, eg.ORDER_NOT_FOUND, st.SOURCE_OMS),
                (eg.CARRIER_NODE, eg.ORDER_NOT_FOUND, st.SOURCE_CARRIER),
            ],
        )
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)

    def test_state_cannot_supply_identity(self):
        state = initial_state("SYN-ORD-0004")
        state["access_context"] = access()  # not a state key; must not authorize
        out = run("SYN-ORD-0004", context=None, state=state)
        self.assertNotIn("access_context", out)
        self.assert_denied(out, eg.NODE_ORDER)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class UnverifiedEvidenceTests(unittest.TestCase):
    def assert_unverified(self, out, reason):
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(out["carrier_evidence"]["verification"], DeliveryStatus.UNVERIFIED)
        self.assertEqual(out["carrier_evidence"]["reason"], reason)
        self.assertEqual(out["carrier_evidence"]["payload"]["current_delivery_status"]["status"], "UNKNOWN")
        self.assertIsNone(out["carrier_evidence"]["payload"]["current_delivery_status"]["carrier_estimated_delivery"])
        self.assertIn((eg.CARRIER_NODE, reason, st.SOURCE_CARRIER), codes(out))
        self.assertIs(out["requires_human_review"], True)
        validate_state(out)

    def test_carrier_scenarios_stay_unverified(self):
        for order_id, reason in (
            ("SYN-ORD-0005", "STALE_CARRIER_DATA"),
            ("SYN-ORD-0006", "CARRIER_UNAVAILABLE"),
            ("SYN-ORD-0007", "CARRIER_RECORD_NOT_FOUND"),
            ("SYN-ORD-0002", "NO_TRACKING_NUMBER_IN_OMS"),
        ):
            with self.subTest(order_id=order_id):
                self.assert_unverified(run(order_id), reason)

    def test_unavailable_carrier_has_no_invented_provenance(self):
        evidence = run("SYN-ORD-0006")["carrier_evidence"]
        self.assertIsNone(evidence["source"])
        self.assertIsNone(evidence["source_as_of"])
        self.assertEqual(evidence["payload"]["tracking_events"], [])

    def test_malformed_carrier_response_is_unverified(self):
        out = run("SYN-ORD-0004", carrier_adapter=_WrongTrackingAdapter())
        self.assert_unverified(out, "MALFORMED_CARRIER_RESPONSE")

    def test_adapter_exception_is_unverified_without_its_text(self):
        out = run("SYN-ORD-0004", carrier_adapter=_RaisingAdapter())
        self.assert_unverified(out, "CARRIER_ADAPTER_ERROR")
        self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))

    def test_stale_by_tight_freshness_limit_is_unverified(self):
        out = run("SYN-ORD-0004", freshness_limit=timedelta(minutes=1))
        self.assert_unverified(out, "STALE_CARRIER_DATA")


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class ErrorPropagationTests(unittest.TestCase):
    def test_unexpected_oms_fault_is_recorded_without_text(self):
        with mock.patch.object(eg, "get_order_details", side_effect=RuntimeError(SECRET_TEXT)):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.ERROR)
        self.assertIsNone(out["oms_evidence"]["payload"])
        self.assertEqual(codes(out), [(eg.OMS_NODE, eg.OMS_RETRIEVAL_ERROR, st.SOURCE_OMS)])
        self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))
        # The carrier node still runs on its own authorization (no routing yet).
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)

    def test_invalid_oms_result_is_not_stored(self):
        with mock.patch.object(eg, "get_order_details", return_value={"session_token": "x"}):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.ERROR)
        self.assertIsNone(out["oms_evidence"]["payload"])
        self.assertIn((eg.OMS_NODE, eg.INVALID_OMS_EVIDENCE, st.SOURCE_OMS), codes(out))

    def test_invalid_carrier_evidence_is_unverified(self):
        with mock.patch.object(eg, "retrieve_carrier_evidence", side_effect=InvalidStateError("x")):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["carrier_evidence"]["retrieval_status"], st.ERROR)
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIn((eg.CARRIER_NODE, eg.INVALID_CARRIER_EVIDENCE, st.SOURCE_CARRIER), codes(out))

    def test_unexpected_carrier_fault_is_recorded_without_text(self):
        with mock.patch.object(eg, "retrieve_carrier_evidence", side_effect=RuntimeError(SECRET_TEXT)):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["carrier_evidence"]["retrieval_status"], st.ERROR)
        self.assertIn((eg.CARRIER_NODE, eg.CARRIER_RETRIEVAL_ERROR, st.SOURCE_CARRIER), codes(out))
        self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))

    def test_forged_verified_input_state_stops_the_run(self):
        state = initial_state("SYN-ORD-0004")
        state["delivery_status"] = DeliveryStatus.VERIFIED  # no trusted evidence
        with self.assertRaises(InvalidStateError):
            run("SYN-ORD-0004", state=state)

    def test_forged_unattested_carrier_evidence_cannot_verify(self):
        forged = st._build_evidence(
            st.SOURCE_CARRIER, st.RETRIEVED, verification=DeliveryStatus.VERIFIED, reason=None,
            source="SYN-SRC", retrieved_at=NOW.isoformat(), source_as_of=NOW.isoformat(),
            freshness_status="FRESH", age_seconds=0, limit_seconds=60, payload={},
        )
        forged["attestation"] = "0" * 64
        with mock.patch.object(eg, "retrieve_carrier_evidence", return_value=forged):
            with self.assertRaises(InvalidStateError):
                run("SYN-ORD-0004")


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class StateMergingTests(unittest.TestCase):
    def test_errors_from_both_nodes_append_after_existing_errors(self):
        state = initial_state("SYN-ORD-0004")
        earlier = make_safe_error("earlier_step", "EARLIER_CODE")
        state["errors"] = [earlier]
        out = run("SYN-ORD-0004", context=None, state=state)
        self.assertEqual(out["errors"][0], earlier)
        self.assertEqual([e["node"] for e in out["errors"][1:]], list(eg.NODE_ORDER))

    def test_nodes_write_only_their_own_keys(self):
        updates = list(
            graph().stream(initial_state("SYN-ORD-0004"), context=RUNTIME, stream_mode="updates")
        )
        self.assertEqual(set(updates[0][eg.OMS_NODE]), {"oms_evidence"})
        self.assertEqual(set(updates[1][eg.CARRIER_NODE]), {"carrier_evidence", "delivery_status"})

    def test_human_review_and_order_id_are_preserved(self):
        for order_id in ("SYN-ORD-0004", "SYN-ORD-0006", "SYN-ORD-0003"):
            with self.subTest(order_id=order_id):
                out = run(order_id)
                self.assertEqual(out["order_id"], order_id)
                self.assertIs(out["requires_human_review"], True)
                self.assertIsNone(out["final_investigation"])

    def test_invalid_order_id_in_state_stops_the_run(self):
        state = initial_state("SYN-ORD-0004")
        state["order_id"] = "not-an-order"
        with self.assertRaises(InvalidStateError):
            run("SYN-ORD-0004", state=state)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class RuntimeContextIsolationTests(unittest.TestCase):
    def test_runtime_context_is_not_in_output_state(self):
        out = run("SYN-ORD-0004")
        self.assertEqual(set(out), set(st.InvestigationState.__annotations__))
        dumped = json.dumps(out, default=str)
        ctx = RUNTIME.access_context
        for value in (ctx.user_id, ctx.tenant_id, *ctx.user_permissions, *ctx.service_scopes):
            self.assertNotIn(value, dumped)
        self.assertNotIn(st._ATTESTATION_KEY.hex(), dumped)

    def test_runtime_context_is_not_in_streamed_values(self):
        ctx = RUNTIME.access_context
        for snapshot in graph().stream(initial_state("SYN-ORD-0004"), context=RUNTIME, stream_mode="values"):
            dumped = json.dumps(snapshot, default=str)
            self.assertNotIn(ctx.user_id, dumped)
            self.assertNotIn(ctx.tenant_id, dumped)
            self.assertFalse(any(isinstance(v, (AccessContext, InvestigationRuntimeContext)) for v in snapshot.values()))

    def test_no_network_adapter_is_used_by_default(self):
        # Default wiring is the in-memory synthetic adapter.
        with mock.patch("socket.socket", side_effect=AssertionError("network used")):
            out = eg.build_evidence_graph().invoke(initial_state("SYN-ORD-0004"), context=RUNTIME)
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
