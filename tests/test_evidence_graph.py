"""Unit tests for the Steps 3.3 to 3.5 OMS-first evidence graph.

Covers OMS retrieval, conditional routing, the parallel carrier and OMS
analysis branches and the fan-in barrier. Tests never assume an order
between the two parallel branches.

Synthetic data and in-memory adapters only. No AWS, Bedrock, carrier or
other network calls. Skipped when the optional ``langgraph`` extra is not
installed (``pip install .[langgraph]``).
"""

import contextlib
import copy
import importlib.util
import json
import threading
import time
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
    AccessDeniedError,
)
from northstar.tools.order_details import InvalidToolInputError, OrderNotFoundError, get_order_details

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


def executed_nodes(order_id="SYN-ORD-0004", context=RUNTIME, state=None, **kwargs):
    updates = graph(**kwargs).stream(state or initial_state(order_id), context=context, stream_mode="updates")
    return [next(iter(u)) for u in updates]


def assert_fanout_run(test, nodes):
    """OMS first, both parallel branches once each (any order), then the join."""
    test.assertEqual(len(nodes), 4)
    test.assertEqual(nodes[0], eg.OMS_NODE)
    test.assertCountEqual(nodes[1:3], eg.PARALLEL_NODES)
    test.assertEqual(nodes[3], eg.JOIN_NODE)


def oms_payload(order_id="SYN-ORD-0004"):
    return {
        "order_id": order_id,
        "order_status": "SHIPPED",
        "promised_delivery_date": "2026-11-09",
        "carrier": "SYN-CARRIER",
    }


def state_with_oms(status=st.RETRIEVED, order_id="SYN-ORD-0004", **overrides):
    """A state as it would look after the OMS node; ``overrides`` forge evidence fields."""
    state = initial_state(order_id)
    if status == st.RETRIEVED:
        record = st.make_evidence(
            st.SOURCE_OMS, st.RETRIEVED, retrieved_at=NOW.isoformat(), payload=oms_payload(order_id)
        )
    else:
        record = st.make_evidence(st.SOURCE_OMS, status, reason="SOME_CODE")
    record.update(overrides)
    state["oms_evidence"] = record
    return state


class _RaisingAdapter:
    def get_tracking(self, carrier, tracking_number):
        raise RuntimeError(SECRET_TEXT)


class _WrongTrackingAdapter:
    """Returns a well-typed response for a different shipment (malformed)."""

    def get_tracking(self, carrier, tracking_number):
        return CarrierTrackingResponse(carrier, "SYN-TRK-9999", "SYN-SRC", NOW, (), (), None)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class GraphStructureTests(unittest.TestCase):
    def test_oms_fans_out_conditionally_and_joins(self):
        drawn = graph().get_graph()
        self.assertEqual(
            set(drawn.nodes),
            {"__start__", "__end__", eg.OMS_NODE, eg.CARRIER_NODE, eg.ANALYSIS_NODE, eg.JOIN_NODE},
        )
        edges = [(e.source, e.target, e.conditional) for e in drawn.edges]
        self.assertCountEqual(
            edges,
            [
                ("__start__", eg.OMS_NODE, False),
                (eg.OMS_NODE, eg.CARRIER_NODE, True),
                (eg.OMS_NODE, eg.ANALYSIS_NODE, True),
                (eg.OMS_NODE, "__end__", True),
                (eg.CARRIER_NODE, eg.JOIN_NODE, False),
                (eg.ANALYSIS_NODE, eg.JOIN_NODE, False),
                (eg.JOIN_NODE, "__end__", False),
            ],
        )

    def test_join_is_an_explicit_waiting_edge_on_both_branches(self):
        builder = graph().builder
        self.assertEqual(builder.waiting_edges, {(eg.PARALLEL_NODES, eg.JOIN_NODE)})
        # Neither branch has its own direct edge to the join or to END.
        self.assertEqual(builder.edges, {("__start__", eg.OMS_NODE), (eg.JOIN_NODE, "__end__")})

    def test_route_map_has_only_the_branches_and_end(self):
        self.assertEqual(
            eg.OMS_ROUTES,
            {eg.CARRIER_NODE: eg.CARRIER_NODE, eg.ANALYSIS_NODE: eg.ANALYSIS_NODE, eg.END: eg.END},
        )
        self.assertCountEqual(eg.ROUTE_TO_EVIDENCE, (eg.CARRIER_NODE, eg.ANALYSIS_NODE))

    def test_each_node_runs_once(self):
        assert_fanout_run(self, executed_nodes("SYN-ORD-0004"))

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

    def assert_oms_denied_and_ended(self, out):
        # OMS denied -> END: the carrier node never ran, so no carrier evidence.
        self.assert_denied(out, (eg.OMS_NODE,))
        self.assertIsNone(out["carrier_evidence"])
        self.assertEqual(codes(out), [(eg.OMS_NODE, eg.ACCESS_DENIED, st.SOURCE_OMS)])

    def test_missing_runtime_context_denies_oms_and_ends(self):
        self.assert_oms_denied_and_ended(run("SYN-ORD-0004", context=None))

    def test_wrong_context_type_is_denied(self):
        # A bare AccessContext is not the trusted runtime context wrapper.
        self.assert_oms_denied_and_ended(run("SYN-ORD-0004", context=access()))

    def test_missing_carrier_permission_denies_only_carrier(self):
        ctx = InvestigationRuntimeContext(access(user_perms=(OMS_ORDER_READ,)))
        out = run("SYN-ORD-0004", context=ctx)
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.RETRIEVED)
        self.assert_denied(out, (eg.CARRIER_NODE,))
        self.assertEqual(codes(out), [(eg.CARRIER_NODE, eg.ACCESS_DENIED, st.SOURCE_CARRIER)])

    def test_missing_service_scope_is_denied(self):
        ctx = InvestigationRuntimeContext(access(scopes=(CARRIER_READ_SCOPE,)))
        self.assert_oms_denied_and_ended(run("SYN-ORD-0004", context=ctx))

    def test_other_tenant_order_is_not_found_without_data(self):
        out = run("SYN-ORD-0003")  # tenant B order, tenant A user
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.NOT_FOUND)
        self.assertIsNone(out["oms_evidence"]["payload"])
        self.assertIsNone(out["carrier_evidence"])  # routed to END
        self.assertEqual(codes(out), [(eg.OMS_NODE, eg.ORDER_NOT_FOUND, st.SOURCE_OMS)])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(out["requires_human_review"], True)

    def test_state_cannot_supply_identity(self):
        state = initial_state("SYN-ORD-0004")
        state["access_context"] = access()  # not a state key; must not authorize
        out = run("SYN-ORD-0004", context=None, state=state)
        self.assertNotIn("access_context", out)
        self.assert_oms_denied_and_ended(out)


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
        # Failed OMS routes to END: no carrier call, delivery stays UNVERIFIED.
        self.assertIsNone(out["carrier_evidence"])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(out["requires_human_review"], True)

    def test_invalid_oms_result_is_not_stored(self):
        with mock.patch.object(eg, "get_order_details", return_value={"session_token": "x"}):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.ERROR)
        self.assertIsNone(out["oms_evidence"]["payload"])
        self.assertEqual(codes(out), [(eg.OMS_NODE, eg.INVALID_OMS_EVIDENCE, st.SOURCE_OMS)])
        self.assertIsNone(out["carrier_evidence"])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)

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
        # OMS succeeds, carrier is denied: both nodes run, errors append in order.
        state = initial_state("SYN-ORD-0004")
        earlier = make_safe_error("earlier_step", "EARLIER_CODE")
        state["errors"] = [earlier]
        ctx = InvestigationRuntimeContext(access(user_perms=(OMS_ORDER_READ,)))
        out = run("SYN-ORD-0004", context=ctx, state=state)
        self.assertEqual(
            out["errors"], [earlier, make_safe_error(eg.CARRIER_NODE, eg.ACCESS_DENIED, source_system=st.SOURCE_CARRIER)]
        )

    def test_safe_exit_appends_after_existing_errors(self):
        state = initial_state("SYN-ORD-0004")
        earlier = make_safe_error("earlier_step", "EARLIER_CODE")
        state["errors"] = [earlier]
        out = run("SYN-ORD-0004", context=None, state=state)
        self.assertEqual(
            out["errors"], [earlier, make_safe_error(eg.OMS_NODE, eg.ACCESS_DENIED, source_system=st.SOURCE_OMS)]
        )
        self.assertIsNone(out["carrier_evidence"])

    def test_nodes_write_only_their_own_keys(self):
        updates = {}
        for update in graph().stream(initial_state("SYN-ORD-0004"), context=RUNTIME, stream_mode="updates"):
            updates.update(update)
        self.assertEqual(set(updates[eg.OMS_NODE]), {"oms_evidence"})
        self.assertEqual(set(updates[eg.CARRIER_NODE]), {"carrier_evidence", "delivery_status"})
        self.assertEqual(set(updates[eg.ANALYSIS_NODE]), {"oms_analysis"})
        self.assertIsNone(updates[eg.JOIN_NODE])  # the barrier writes nothing

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
class ConditionalRoutingGraphTests(unittest.TestCase):
    """Step 3.4: the carrier node runs only after a successful OMS retrieval."""

    def assert_safe_exit(self, out, oms_status, code):
        self.assertEqual(out["oms_evidence"]["retrieval_status"], oms_status)
        self.assertIsNone(out["oms_evidence"]["payload"])
        self.assertIsNone(out["carrier_evidence"])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(effective_delivery_status(out), DeliveryStatus.UNVERIFIED)
        self.assertIs(out["requires_human_review"], True)
        self.assertIsNone(out["final_investigation"])
        self.assertEqual(codes(out), [(eg.OMS_NODE, code, st.SOURCE_OMS)])
        self.assertTrue(all(e["retryable"] is False for e in out["errors"]))
        validate_state(out)

    def test_successful_oms_routes_to_carrier(self):
        assert_fanout_run(self, executed_nodes("SYN-ORD-0004"))
        out = run("SYN-ORD-0004")
        self.assertEqual(out["oms_evidence"]["retrieval_status"], st.RETRIEVED)
        self.assertTrue(is_trusted_carrier_evidence(out["carrier_evidence"]))
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)

    def test_successful_oms_with_unverified_carrier_still_routes_to_carrier(self):
        # Routing to carrier is not verification: the carrier result decides.
        assert_fanout_run(self, executed_nodes("SYN-ORD-0006"))
        out = run("SYN-ORD-0006")
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIs(out["requires_human_review"], True)

    def test_access_denied_routes_to_end(self):
        self.assertEqual(executed_nodes(context=None), [eg.OMS_NODE])
        self.assert_safe_exit(run("SYN-ORD-0004", context=None), st.DENIED, eg.ACCESS_DENIED)

    def test_order_not_found_routes_to_end(self):
        self.assertEqual(executed_nodes("SYN-ORD-0003"), [eg.OMS_NODE])
        self.assert_safe_exit(run("SYN-ORD-0003"), st.NOT_FOUND, eg.ORDER_NOT_FOUND)

    def test_failed_oms_retrievals_route_to_end(self):
        for exc, status, code in (
            (AccessDeniedError(SECRET_TEXT), st.DENIED, eg.ACCESS_DENIED),
            (OrderNotFoundError(SECRET_TEXT), st.NOT_FOUND, eg.ORDER_NOT_FOUND),
            (InvalidToolInputError(SECRET_TEXT), st.ERROR, eg.INVALID_ORDER_ID),
            (InvalidStateError(SECRET_TEXT), st.ERROR, eg.INVALID_OMS_EVIDENCE),
            (RuntimeError(SECRET_TEXT), st.ERROR, eg.OMS_RETRIEVAL_ERROR),
        ):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(eg, "get_order_details", side_effect=exc):
                    self.assertEqual(executed_nodes(), [eg.OMS_NODE])
                    out = run("SYN-ORD-0004")
                self.assert_safe_exit(out, status, code)
                self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))

    def test_carrier_is_not_invoked_on_safe_exit(self):
        cases = (
            ({"context": None}, "SYN-ORD-0004"),
            ({}, "SYN-ORD-0003"),
            ({"context": InvestigationRuntimeContext(access(scopes=(CARRIER_READ_SCOPE,)))}, "SYN-ORD-0004"),
        )
        for kwargs, order_id in cases:
            with self.subTest(order_id=order_id, kwargs=list(kwargs)):
                with mock.patch.object(eg, "retrieve_carrier_evidence") as carrier, mock.patch(
                    "northstar.orchestration.multi_agent.state.get_carrier_tracking"
                ) as tool:
                    out = run(order_id, **kwargs)
                carrier.assert_not_called()
                tool.assert_not_called()
                self.assertIsNone(out["carrier_evidence"])

    def test_carrier_is_invoked_once_on_success(self):
        with mock.patch.object(eg, "retrieve_carrier_evidence", wraps=st.retrieve_carrier_evidence) as carrier:
            out = run("SYN-ORD-0004")
        carrier.assert_called_once()
        self.assertIs(carrier.call_args.kwargs["runtime_context"], RUNTIME)
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)

    def test_preexisting_errors_are_preserved_on_safe_exit(self):
        state = initial_state("SYN-ORD-0003")
        earlier = make_safe_error("earlier_step", "EARLIER_CODE", source_system=st.SOURCE_OMS)
        state["errors"] = [earlier]
        out = run("SYN-ORD-0003", state=state)
        self.assertEqual(out["errors"][0], earlier)
        self.assertEqual(codes(out)[1:], [(eg.OMS_NODE, eg.ORDER_NOT_FOUND, st.SOURCE_OMS)])

    def test_caller_route_hints_do_not_reach_the_carrier(self):
        # LangGraph drops input keys outside the state schema, so a route
        # hint never reaches the router; routing follows the OMS result only.
        # (The router itself rejects such keys: RouteAfterOmsTests.)
        for key in ("route", "next", "oms_ok", "error"):
            with self.subTest(key=key):
                state = initial_state("SYN-ORD-0003")
                state[key] = eg.CARRIER_NODE
                self.assertEqual(executed_nodes("SYN-ORD-0003", state=state), [eg.OMS_NODE])
                out = run("SYN-ORD-0003", state=state)
                self.assertNotIn(key, out)
                self.assertIsNone(out["carrier_evidence"])

    def test_oms_payload_without_required_fields_stops_the_run(self):
        with mock.patch.object(eg, "get_order_details", return_value={"order_id": "SYN-ORD-0004"}):
            with mock.patch.object(eg, "retrieve_carrier_evidence") as carrier:
                with self.assertRaises(InvalidStateError):
                    run("SYN-ORD-0004")
        carrier.assert_not_called()

    def test_oms_payload_for_another_order_stops_the_run(self):
        with mock.patch.object(eg, "get_order_details", return_value=oms_payload("SYN-ORD-0001")):
            with mock.patch.object(eg, "retrieve_carrier_evidence") as carrier:
                with self.assertRaises(InvalidStateError):
                    run("SYN-ORD-0004")
        carrier.assert_not_called()


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class RouteAfterOmsTests(unittest.TestCase):
    """The routing function in isolation: pure, deterministic, fail closed."""

    def test_retrieved_usable_oms_routes_to_carrier(self):
        self.assertEqual(eg.route_after_oms(state_with_oms()), eg.ROUTE_TO_EVIDENCE)

    def test_unsuccessful_statuses_route_to_end(self):
        for status in (st.DENIED, st.NOT_FOUND, st.ERROR, st.UNAVAILABLE):
            with self.subTest(status=status):
                self.assertEqual(eg.route_after_oms(state_with_oms(status)), eg.ROUTE_TO_END)

    def test_routing_is_pure_and_deterministic(self):
        for state in (state_with_oms(), state_with_oms(st.DENIED)):
            before = copy.deepcopy(state)
            results = {eg.route_after_oms(state) for _ in range(3)}
            self.assertEqual(len(results), 1)
            self.assertEqual(state, before)

    def test_errors_and_delivery_status_do_not_drive_routing(self):
        state = state_with_oms(st.DENIED)
        self.assertEqual(eg.route_after_oms(state), eg.ROUTE_TO_END)  # no error items needed
        state = state_with_oms()
        state["errors"] = [make_safe_error(eg.OMS_NODE, "ROUTE_TO_END", source_system=st.SOURCE_OMS)]
        self.assertEqual(eg.route_after_oms(state), eg.ROUTE_TO_EVIDENCE)

    def test_missing_oms_evidence_is_rejected(self):
        with self.assertRaises(InvalidStateError):
            eg.route_after_oms(initial_state("SYN-ORD-0004"))

    def test_forged_or_malformed_state_is_rejected(self):
        carrier_in_oms_slot = state_with_oms(source_system=st.SOURCE_CARRIER)
        forged_verified = state_with_oms()
        forged_verified["delivery_status"] = DeliveryStatus.VERIFIED
        route_flag = state_with_oms(st.DENIED)
        route_flag["route"] = eg.CARRIER_NODE
        context_in_state = state_with_oms()
        context_in_state["access_context"] = access()
        human_review_cleared = state_with_oms()
        human_review_cleared["requires_human_review"] = False
        cases = {
            "not a mapping": [("oms_evidence", state_with_oms()["oms_evidence"])],
            "carrier record in OMS slot": carrier_in_oms_slot,
            "forged VERIFIED delivery_status": forged_verified,
            "route flag in state": route_flag,
            "access context in state": context_in_state,
            "human review cleared": human_review_cleared,
            "OMS claims VERIFIED": state_with_oms(verification=DeliveryStatus.VERIFIED),
            "unknown retrieval status": state_with_oms(retrieval_status="OK"),
            "verification is a string": state_with_oms(verification="VERIFIED"),
            "extra evidence field": state_with_oms(route=eg.CARRIER_NODE),
            "RETRIEVED without payload": state_with_oms(payload=None),
            "RETRIEVED with list payload": state_with_oms(payload=[oms_payload()]),
            "RETRIEVED missing fields": state_with_oms(payload={"order_id": "SYN-ORD-0004"}),
            "RETRIEVED non-string field": state_with_oms(payload={**oms_payload(), "carrier": None}),
            "RETRIEVED with extra field": state_with_oms(payload={**oms_payload(), "note": "x"}),
            "RETRIEVED payload without order_id": state_with_oms(
                payload={k: v for k, v in oms_payload().items() if k != "order_id"}
            ),
            "RETRIEVED for another order": state_with_oms(payload=oms_payload("SYN-ORD-0001")),
            "RETRIEVED with failure reason": state_with_oms(reason="ACCESS_DENIED"),
            "RETRIEVED with forged attestation": state_with_oms(attestation="0" * 64),
            "RETRIEVED with credential key": state_with_oms(payload={**oms_payload(), "api_token": "x"}),
            "DENIED with payload": state_with_oms(st.DENIED, payload=oms_payload()),
        }
        for name, state in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(InvalidStateError):
                    eg.route_after_oms(state)

    def test_payload_fields_match_the_oms_tool_contract(self):
        # Guards against drift between the router and get_order_details.
        for order_id in ("SYN-ORD-0002", "SYN-ORD-0004", "SYN-ORD-0006"):
            with self.subTest(order_id=order_id):
                result = get_order_details(order_id, access_context=access())
                self.assertEqual(set(result), eg.OMS_PAYLOAD_FIELDS)
                self.assertTrue(all(isinstance(v, str) for v in result.values()))
                self.assertEqual(set(oms_payload(order_id)), eg.OMS_PAYLOAD_FIELDS)
                state = initial_state(order_id)
                state["oms_evidence"] = st.make_evidence(
                    st.SOURCE_OMS, st.RETRIEVED, retrieved_at=NOW.isoformat(), payload=result
                )
                self.assertEqual(eg.route_after_oms(state), eg.ROUTE_TO_EVIDENCE)

    def test_validate_state_rejects_unexpected_keys_directly(self):
        for key in ("route", "access_context", "credentials"):
            with self.subTest(key=key):
                state = state_with_oms()
                state[key] = eg.CARRIER_NODE
                with self.assertRaises(InvalidStateError):
                    validate_state(state)

    def test_rejection_messages_carry_no_values(self):
        state = state_with_oms(payload={**oms_payload(), "carrier": None})
        state["oms_evidence"]["payload"]["order_status"] = SECRET_TEXT
        with self.assertRaises(InvalidStateError) as raised:
            eg.route_after_oms(state)
        self.assertNotIn(SECRET_TEXT, str(raised.exception))


def expected_analysis(promised, at=NOW):
    return {
        "analyzed_at": at.isoformat(),
        "promised_delivery_date": promised,
        "days_after_promised_date": (at.date() - datetime.fromisoformat(promised).date()).days,
    }


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class ParallelExecutionTests(unittest.TestCase):
    """Step 3.5: carrier retrieval and OMS analysis run in parallel, then fan in."""

    def test_branches_are_scheduled_in_the_same_superstep(self):
        steps = {}
        for event in graph().stream(initial_state("SYN-ORD-0004"), context=RUNTIME, stream_mode="debug"):
            if event["type"] == "task":
                steps.setdefault(event["payload"]["name"], []).append(event["step"])
        self.assertEqual(set(steps), {eg.OMS_NODE, *eg.PARALLEL_NODES, eg.JOIN_NODE})
        self.assertTrue(all(len(v) == 1 for v in steps.values()))  # each node once
        (oms,), (carrier,), (analysis,), (join,) = (
            steps[eg.OMS_NODE], steps[eg.CARRIER_NODE], steps[eg.ANALYSIS_NODE], steps[eg.JOIN_NODE]
        )
        self.assertEqual(carrier, analysis)
        self.assertEqual(carrier, oms + 1)
        self.assertEqual(join, carrier + 1)

    def test_branches_actually_run_concurrently(self):
        # A two-party barrier only opens if both branches are inside it at the
        # same time; sequential execution would time out and break it.
        barrier = threading.Barrier(2, timeout=5)
        passed = []

        def rendezvous(name, real):
            def call(*args, **kwargs):
                barrier.wait()
                passed.append((name, threading.get_ident()))
                return real(*args, **kwargs)
            return call

        carrier = rendezvous("carrier", st.retrieve_carrier_evidence)
        analysis = rendezvous("analysis", st.derive_oms_analysis)
        with mock.patch.object(eg, "retrieve_carrier_evidence", carrier), \
                mock.patch.object(eg, "derive_oms_analysis", analysis):
            out = run("SYN-ORD-0004")
        self.assertFalse(barrier.broken)
        self.assertCountEqual([name for name, _ in passed], ["carrier", "analysis"])
        self.assertEqual(len({ident for _, ident in passed}), 2)
        self.assertEqual(out["errors"], [])
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)
        self.assertIsNotNone(out["oms_analysis"])

    def test_join_runs_once_after_both_branches(self):
        seen = []
        real_join = eg.validate_merged_evidence

        def spy(state):
            seen.append(copy.deepcopy(dict(state)))
            return real_join(state)

        real_derive = st.derive_oms_analysis

        def slow_derive(*args, **kwargs):
            time.sleep(0.2)  # the join must still wait for this branch
            return real_derive(*args, **kwargs)

        with mock.patch.object(eg, "validate_merged_evidence", spy), \
                mock.patch.object(eg, "derive_oms_analysis", slow_derive):
            out = run("SYN-ORD-0004")
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0]["carrier_evidence"])
        self.assertIsNotNone(seen[0]["oms_analysis"])
        self.assertEqual(seen[0]["oms_analysis"], out["oms_analysis"])

    def test_successful_merge_combines_both_branches(self):
        direct = get_order_details("SYN-ORD-0004", access_context=access())
        out = run("SYN-ORD-0004")
        validate_state(out)
        self.assertEqual(out["oms_evidence"]["payload"], direct)  # evidence unchanged
        self.assertEqual(out["oms_analysis"], expected_analysis(direct["promised_delivery_date"]))
        self.assertEqual(out["oms_analysis"]["days_after_promised_date"], -1)
        self.assertTrue(is_trusted_carrier_evidence(out["carrier_evidence"]))
        self.assertIs(out["delivery_status"], DeliveryStatus.VERIFIED)
        self.assertEqual(out["errors"], [])
        self.assertIs(out["requires_human_review"], True)
        self.assertIsNone(out["final_investigation"])

    def test_analysis_never_verifies_or_changes_delivery_status(self):
        # Promised date has passed (SYN-ORD-0005) but the carrier is stale:
        # the analysis fact is recorded, delivery stays UNVERIFIED.
        out = run("SYN-ORD-0005")
        self.assertEqual(out["oms_analysis"]["days_after_promised_date"], 4)
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertEqual(codes(out), [(eg.CARRIER_NODE, "STALE_CARRIER_DATA", st.SOURCE_CARRIER)])
        self.assertNotIn("verification", out["oms_analysis"])

    def test_errors_from_both_branches_merge_without_ordering(self):
        state = initial_state("SYN-ORD-0004")
        earlier = make_safe_error("earlier_step", "EARLIER_CODE")
        state["errors"] = [earlier]
        ctx = InvestigationRuntimeContext(access(user_perms=(OMS_ORDER_READ,)))
        with mock.patch.object(eg, "derive_oms_analysis", side_effect=RuntimeError(SECRET_TEXT)):
            out = run("SYN-ORD-0004", context=ctx, state=state)
        self.assertEqual(out["errors"][0], earlier)  # earlier errors are never reordered
        self.assertCountEqual(
            out["errors"][1:],
            [
                make_safe_error(eg.CARRIER_NODE, eg.ACCESS_DENIED, source_system=st.SOURCE_CARRIER),
                make_safe_error(eg.ANALYSIS_NODE, eg.OMS_ANALYSIS_ERROR, source_system=st.SOURCE_OMS),
            ],
        )
        self.assertIsNone(out["oms_analysis"])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))
        validate_state(out)


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class ParallelFailClosedTests(unittest.TestCase):
    def test_failed_oms_skips_both_branches_and_the_join(self):
        cases = (
            ({"context": None}, "SYN-ORD-0004", None),
            ({}, "SYN-ORD-0003", None),
            ({}, "SYN-ORD-0004", RuntimeError(SECRET_TEXT)),
            ({}, "SYN-ORD-0004", InvalidStateError(SECRET_TEXT)),
        )
        for kwargs, order_id, oms_fault in cases:
            with self.subTest(order_id=order_id, kwargs=list(kwargs), fault=type(oms_fault).__name__):
                with contextlib.ExitStack() as stack:
                    carrier = stack.enter_context(mock.patch.object(eg, "retrieve_carrier_evidence"))
                    derive = stack.enter_context(mock.patch.object(eg, "derive_oms_analysis"))
                    if oms_fault is not None:
                        stack.enter_context(mock.patch.object(eg, "get_order_details", side_effect=oms_fault))
                    nodes = executed_nodes(order_id, **kwargs)
                    out = run(order_id, **kwargs)
                self.assertEqual(nodes, [eg.OMS_NODE])
                carrier.assert_not_called()
                derive.assert_not_called()
                self.assertIsNone(out["oms_analysis"])
                self.assertIsNone(out["carrier_evidence"])
                self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
                self.assertIs(out["requires_human_review"], True)
                self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))

    def test_invalid_oms_evidence_never_reaches_the_branches(self):
        with mock.patch.object(eg, "get_order_details", return_value=oms_payload("SYN-ORD-0001")), \
                mock.patch.object(eg, "retrieve_carrier_evidence") as carrier, \
                mock.patch.object(eg, "derive_oms_analysis") as derive:
            with self.assertRaises(InvalidStateError):
                run("SYN-ORD-0004")
        carrier.assert_not_called()
        derive.assert_not_called()

    def test_carrier_failure_keeps_the_analysis(self):
        for kwargs, code in (
            ({"carrier_adapter": _RaisingAdapter()}, "CARRIER_ADAPTER_ERROR"),
            ({"carrier_adapter": _WrongTrackingAdapter()}, "MALFORMED_CARRIER_RESPONSE"),
        ):
            with self.subTest(code=code):
                assert_fanout_run(self, executed_nodes(**kwargs))
                out = run("SYN-ORD-0004", **kwargs)
                self.assertEqual(out["oms_analysis"], expected_analysis("2026-11-11"))
                self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)
                self.assertEqual(codes(out), [(eg.CARRIER_NODE, code, st.SOURCE_CARRIER)])
                self.assertNotIn(SECRET_TEXT, json.dumps(out, default=str))
        with mock.patch.object(eg, "retrieve_carrier_evidence", side_effect=RuntimeError(SECRET_TEXT)):
            out = run("SYN-ORD-0004")
        self.assertEqual(out["carrier_evidence"]["retrieval_status"], st.ERROR)
        self.assertIsNotNone(out["oms_analysis"])
        self.assertEqual(codes(out), [(eg.CARRIER_NODE, eg.CARRIER_RETRIEVAL_ERROR, st.SOURCE_CARRIER)])

    def test_carrier_denial_does_not_block_or_widen_the_analysis(self):
        # The analysis reads only OMS evidence this user was authorized for;
        # it calls no tool and needs no carrier permission.
        ctx = InvestigationRuntimeContext(access(user_perms=(OMS_ORDER_READ,)))
        with mock.patch("northstar.orchestration.multi_agent.state.get_carrier_tracking",
                        side_effect=AccessDeniedError("permission")) as tool:
            out = run("SYN-ORD-0004", context=ctx)
        tool.assert_called_once()
        self.assertIs(tool.call_args.kwargs["access_context"], ctx.access_context)
        self.assertEqual(out["carrier_evidence"]["retrieval_status"], st.DENIED)
        self.assertIsNone(out["carrier_evidence"]["payload"])
        self.assertEqual(out["oms_analysis"], expected_analysis("2026-11-11"))
        self.assertEqual(codes(out), [(eg.CARRIER_NODE, eg.ACCESS_DENIED, st.SOURCE_CARRIER)])
        self.assertIs(out["delivery_status"], DeliveryStatus.UNVERIFIED)

    def test_unusable_promised_date_fails_closed_without_a_value(self):
        payload = {**oms_payload(), "promised_delivery_date": "next tuesday"}
        with mock.patch.object(eg, "get_order_details", return_value=payload):
            out = run("SYN-ORD-0004")
        self.assertIsNone(out["oms_analysis"])
        self.assertEqual(out["oms_evidence"]["payload"], payload)  # evidence kept as retrieved
        self.assertIn((eg.ANALYSIS_NODE, eg.INVALID_OMS_ANALYSIS, st.SOURCE_OMS), codes(out))
        self.assertTrue(is_trusted_carrier_evidence(out["carrier_evidence"]))
        validate_state(out)

    def test_forged_analysis_in_input_state_stops_the_run(self):
        forged = initial_state("SYN-ORD-0004")
        forged["oms_analysis"] = expected_analysis("2026-11-11")  # no OMS evidence behind it
        with self.assertRaises(InvalidStateError):
            run("SYN-ORD-0004", state=forged)

    def test_join_rejects_an_incomplete_or_invalid_merge(self):
        complete = run("SYN-ORD-0004")
        self.assertEqual(eg.validate_merged_evidence(complete), {})
        no_carrier = {**complete, "carrier_evidence": None}
        oms_failed = {**state_with_oms(st.DENIED), "carrier_evidence": complete["carrier_evidence"]}
        tampered = copy.deepcopy(complete)
        tampered["oms_analysis"]["days_after_promised_date"] += 1
        forged_verified = {**complete, "carrier_evidence": {**complete["carrier_evidence"], "source": "X"}}
        extra_key = {**complete, "route": eg.JOIN_NODE}
        for name, state in {
            "missing carrier evidence": no_carrier,
            "OMS not RETRIEVED": oms_failed,
            "tampered analysis": tampered,
            "tampered VERIFIED carrier evidence": forged_verified,
            "unexpected key": extra_key,
        }.items():
            with self.subTest(case=name), self.assertRaises(InvalidStateError):
                eg.validate_merged_evidence(state)

    def test_concurrent_writes_to_single_writer_fields_are_rejected(self):
        # Writer ownership is enforced by LangGraph itself for keys without a
        # reducer: two writers in one step raise instead of merging.
        from langgraph.errors import InvalidUpdateError
        from langgraph.graph import END, START, StateGraph

        for key, value in (("oms_analysis", None), ("delivery_status", DeliveryStatus.UNVERIFIED)):
            with self.subTest(key=key):
                builder = StateGraph(st.InvestigationState)
                builder.add_node("first", lambda s, k=key, v=value: {k: v})
                builder.add_node("second", lambda s, k=key, v=value: {k: v})
                builder.add_edge(START, "first")
                builder.add_edge(START, "second")
                builder.add_edge(["first", "second"], END)
                with self.assertRaises(InvalidUpdateError):
                    builder.compile().invoke(initial_state("SYN-ORD-0004"))


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
