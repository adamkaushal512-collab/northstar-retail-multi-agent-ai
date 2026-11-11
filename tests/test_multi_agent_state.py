"""Unit tests for the multi-agent investigation state contract.

Synthetic data only. No LangGraph, AWS, Bedrock or network calls; the
reducers are exercised directly the way a LangGraph channel would apply them.
"""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import get_type_hints
from unittest import mock

from northstar.evidence.investigation_schema import DelayedDeliveryInvestigation, DeliveryStatus
from northstar.orchestration.multi_agent import state as st
from northstar.orchestration.multi_agent.state import (
    InvalidStateError,
    InvestigationRuntimeContext,
    InvestigationState,
    effective_delivery_status,
    initial_state,
    is_trusted_carrier_evidence,
    keep_human_review,
    make_evidence,
    make_safe_error,
    merge_errors,
    retrieve_carrier_evidence,
    validate_evidence,
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
from northstar.integrations.synthetic_carrier import (
    CarrierEvent,
    CarrierTrackingResponse,
    CarrierUnavailableError,
    SyntheticCarrierAdapter,
)
from northstar.tools.carrier_tracking import get_carrier_tracking
from northstar.tools.order_details import OrderNotFoundError, get_order_details

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
CONTEXT = AccessContext(
    "SYN-USER-0001",
    "SYN-TENANT-A",
    frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
)


RUNTIME = InvestigationRuntimeContext(access_context=CONTEXT)


def carrier_result(order_id):
    return get_carrier_tracking(order_id, access_context=CONTEXT, clock=lambda: NOW)


def carrier_evidence(order_id, **kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    return retrieve_carrier_evidence(order_id, runtime_context=RUNTIME, **kwargs)


def verified_state():
    state = initial_state("SYN-ORD-0004")
    state["carrier_evidence"] = carrier_evidence("SYN-ORD-0004")
    state["delivery_status"] = DeliveryStatus.VERIFIED
    return state


def investigation(order_id="SYN-ORD-0004", status="UNVERIFIED"):
    return DelayedDeliveryInvestigation.model_validate_json(
        json.dumps(
            {
                "order_id": order_id,
                "delivery_status": status,
                "summary": "Synthetic summary.",
                "likely_cause": None,
                "evidence": [],
                "missing_information": [],
                "recommended_next_steps": [],
            }
        )
    )


class InitialStateTests(unittest.TestCase):
    def test_initial_state_is_fail_closed(self):
        state = initial_state("SYN-ORD-0001")

        self.assertEqual(state["order_id"], "SYN-ORD-0001")
        self.assertIsNone(state["oms_evidence"])
        self.assertIsNone(state["carrier_evidence"])
        self.assertIs(state["delivery_status"], DeliveryStatus.UNVERIFIED)
        self.assertIsNone(state["final_investigation"])
        self.assertIs(state["requires_human_review"], True)
        self.assertEqual(state["errors"], [])
        self.assertIs(validate_state(state), state)
        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)

    def test_initial_state_has_exactly_the_contract_keys(self):
        self.assertEqual(set(initial_state("SYN-ORD-0001")), set(InvestigationState.__annotations__))

    def test_invalid_order_ids_are_rejected(self):
        for bad in ["", "  ", "ORD-1", "SYN-ORD-1", "SYN-ORD-0001\n", None, 1]:
            with self.subTest(order_id=bad), self.assertRaises(InvalidStateError):
                initial_state(bad)

    def test_reducers_only_on_parallel_safe_fields(self):
        hints = get_type_hints(InvestigationState, include_extras=True)
        reducers = {k: v.__metadata__[0] for k, v in hints.items() if hasattr(v, "__metadata__")}
        self.assertEqual(
            reducers, {"errors": merge_errors, "requires_human_review": keep_human_review}
        )

    def test_state_contains_no_access_context_or_secrets(self):
        names = " ".join(InvestigationState.__annotations__).lower()
        for part in ("access", "context", "token", "credential", "secret", "password"):
            self.assertNotIn(part, names)


class EvidenceTests(unittest.TestCase):
    def test_defaults_are_unverified_and_unknown(self):
        record = make_evidence(st.SOURCE_OMS, st.DENIED)

        self.assertIs(record["verification"], DeliveryStatus.UNVERIFIED)
        self.assertEqual(record["freshness"]["status"], "UNKNOWN")
        self.assertIsNone(record["payload"])
        self.assertIsNone(record["source"])
        self.assertIsNone(record["retrieved_at"])
        self.assertIsNone(record["source_as_of"])

    def test_oms_result_is_preserved_but_never_verifies_delivery(self):
        payload = get_order_details("SYN-ORD-0001", access_context=CONTEXT)
        record = make_evidence(
            st.SOURCE_OMS, st.RETRIEVED, retrieved_at=NOW.isoformat(), payload=payload
        )
        self.assertEqual(record["payload"], payload)
        self.assertIsNot(record["payload"], payload)

        self.assertIs(record["verification"], DeliveryStatus.UNVERIFIED)
        self.assertFalse(is_trusted_carrier_evidence(record))

    def test_verified_carrier_result_keeps_provenance(self):
        result = carrier_result("SYN-ORD-0004")
        record = carrier_evidence("SYN-ORD-0004")

        self.assertIs(record["verification"], DeliveryStatus.VERIFIED)
        self.assertTrue(is_trusted_carrier_evidence(record))
        self.assertEqual(record["source"], result["source"])
        self.assertEqual(record["retrieved_at"], result["retrieved_at"])
        self.assertEqual(record["source_as_of"], result["source_as_of"])
        self.assertEqual(record["freshness"], result["freshness"])
        self.assertEqual(record["payload"], result)

    def test_unverified_carrier_scenarios_stay_unverified(self):
        # Stale, carrier unavailable, no carrier record.
        for order_id in ["SYN-ORD-0005", "SYN-ORD-0006", "SYN-ORD-0007"]:
            with self.subTest(order_id=order_id):
                record = carrier_evidence(order_id)
                self.assertIs(record["verification"], DeliveryStatus.UNVERIFIED)
                self.assertIsNotNone(record["reason"])

    def test_unavailable_carrier_has_no_invented_provenance(self):
        record = carrier_evidence("SYN-ORD-0006")

        self.assertIsNone(record["source"])
        self.assertIsNone(record["source_as_of"])
        self.assertEqual(record["freshness"]["status"], "UNKNOWN")
        self.assertEqual(record["payload"]["tracking_events"], [])

    def test_verified_claim_without_support_is_rejected(self):
        good = carrier_evidence("SYN-ORD-0004")
        tamper = {
            "stale": {"freshness": {**good["freshness"], "status": "STALE"}},
            "no_source": {"source": None},
            "no_as_of": {"source_as_of": None},
            "naive_time": {"retrieved_at": "2026-11-10T12:00:00"},
            "reason_set": {"reason": "STALE_CARRIER_DATA"},
            "no_payload": {"payload": None},
            "not_retrieved": {"retrieval_status": st.UNAVAILABLE},
            "no_attestation": {"attestation": None},
            "bad_attestation": {"attestation": "not-a-digest"},
        }
        for name, change in tamper.items():
            with self.subTest(case=name), self.assertRaises(InvalidStateError):
                validate_evidence({**copy.deepcopy(good), **change})

    def test_carrier_result_claiming_verified_when_stale_is_rejected(self):
        result = carrier_result("SYN-ORD-0004")
        result["current_delivery_status"]["verification"] = "VERIFIED"
        result["freshness"]["status"] = "STALE"
        with self.assertRaises(InvalidStateError):
            st._evidence_from_carrier_result(result)

    def test_malformed_carrier_result_is_rejected(self):
        for bad in [{}, {"current_delivery_status": None}, {"current_delivery_status": {"verification": "MAYBE"}}]:
            with self.subTest(result=bad), self.assertRaises(InvalidStateError):
                st._evidence_from_carrier_result(bad)

    def test_no_retrieval_means_no_data(self):
        for status in [st.NOT_FOUND, st.UNAVAILABLE, st.DENIED, st.ERROR]:
            with self.subTest(status=status):
                with self.assertRaises(InvalidStateError):
                    make_evidence(st.SOURCE_CARRIER, status, payload={"x": 1})
                with self.assertRaises(InvalidStateError):
                    make_evidence(st.SOURCE_CARRIER, status, freshness_status="FRESH")

    def test_invalid_field_values_are_rejected(self):
        cases = {
            "source_system": dict(source_system="SNOWFLAKE", retrieval_status=st.RETRIEVED),
            "retrieval_status": dict(source_system=st.SOURCE_OMS, retrieval_status="OK"),
            "freshness": dict(
                source_system=st.SOURCE_OMS, retrieval_status=st.RETRIEVED, freshness_status="NEW"
            ),
            "age": dict(source_system=st.SOURCE_OMS, retrieval_status=st.RETRIEVED, age_seconds=-1),
            "age_bool": dict(source_system=st.SOURCE_OMS, retrieval_status=st.RETRIEVED, age_seconds=True),
            "reason_text": dict(
                source_system=st.SOURCE_OMS, retrieval_status=st.ERROR, reason="Connection refused: secret"
            ),
            "blank_source": dict(source_system=st.SOURCE_OMS, retrieval_status=st.RETRIEVED, source=" "),
            "bad_time": dict(source_system=st.SOURCE_OMS, retrieval_status=st.RETRIEVED, retrieved_at="soon"),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name), self.assertRaises(InvalidStateError):
                make_evidence(**kwargs)

    def test_payload_rejects_credentials_and_non_json_values(self):
        for payload in [
            {"access_context": "x"},
            {"nested": {"Authorization": "Bearer x"}},
            {"items": [{"api_key": "x"}]},
            {"session_token": "x"},
            {"ctx": CONTEXT},
            {"when": NOW},
            {1: "x"},
        ]:
            with self.subTest(payload=repr(payload)[:40]), self.assertRaises(InvalidStateError):
                make_evidence(st.SOURCE_OMS, st.RETRIEVED, payload=payload)

    def test_error_messages_do_not_echo_values(self):
        with self.assertRaises(InvalidStateError) as caught:
            make_evidence(st.SOURCE_OMS, st.ERROR, reason="leaked detail 1234")
        self.assertNotIn("leaked", str(caught.exception))


class StateValidationTests(unittest.TestCase):
    def test_verified_state_is_valid(self):
        state = verified_state()
        self.assertIs(validate_state(state), state)
        self.assertIs(effective_delivery_status(state), DeliveryStatus.VERIFIED)

    def test_verified_without_evidence_is_rejected_and_reads_unverified(self):
        state = initial_state("SYN-ORD-0004")
        state["delivery_status"] = DeliveryStatus.VERIFIED

        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_verified_with_unverified_evidence_is_rejected(self):
        state = initial_state("SYN-ORD-0006")
        state["carrier_evidence"] = carrier_evidence("SYN-ORD-0006")
        state["delivery_status"] = DeliveryStatus.VERIFIED

        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_tampered_evidence_reads_unverified(self):
        state = verified_state()
        state["carrier_evidence"]["freshness"]["status"] = "STALE"
        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)

    def test_garbage_state_reads_unverified(self):
        for state in [{}, {"delivery_status": "VERIFIED"}, {"delivery_status": DeliveryStatus.VERIFIED, "carrier_evidence": "x"}]:
            with self.subTest(state=state):
                self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)

    def test_unknown_keys_are_rejected(self):
        for key, value in [("access_context", CONTEXT), ("token", "x"), ("credentials", {})]:
            state = initial_state("SYN-ORD-0001")
            state[key] = value
            with self.subTest(key=key), self.assertRaises(InvalidStateError):
                validate_state(state)

    def test_missing_keys_are_rejected(self):
        state = initial_state("SYN-ORD-0001")
        del state["carrier_evidence"]
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_human_review_cannot_be_disabled(self):
        for value in [False, None, 1, "true"]:
            state = initial_state("SYN-ORD-0001")
            state["requires_human_review"] = value
            with self.subTest(value=value), self.assertRaises(InvalidStateError):
                validate_state(state)

    def test_evidence_in_wrong_slot_or_for_other_order_is_rejected(self):
        state = initial_state("SYN-ORD-0004")
        state["oms_evidence"] = carrier_evidence("SYN-ORD-0004")
        with self.assertRaises(InvalidStateError):
            validate_state(state)

        state = initial_state("SYN-ORD-0001")
        state["carrier_evidence"] = carrier_evidence("SYN-ORD-0004")
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_delivery_status_must_be_enum(self):
        state = initial_state("SYN-ORD-0001")
        state["delivery_status"] = "UNVERIFIED"
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_final_investigation_reuses_phase_2_schema(self):
        state = initial_state("SYN-ORD-0004")
        state["final_investigation"] = investigation()
        validate_state(state)

        for bad in [
            {"order_id": "SYN-ORD-0004"},
            investigation(order_id="SYN-ORD-0001"),
            investigation(status="VERIFIED"),  # no verified evidence in state
        ]:
            state["final_investigation"] = bad
            with self.subTest(bad=type(bad).__name__), self.assertRaises(InvalidStateError):
                validate_state(state)

    def test_final_investigation_may_be_verified_with_evidence(self):
        state = verified_state()
        state["final_investigation"] = investigation(status="VERIFIED")
        validate_state(state)


class _RecordingAdapter:
    """Counts calls so tests can prove a denied request retrieved nothing."""

    def __init__(self, inner=None, response=None):
        self.calls = 0
        self.inner = inner
        self.response = response

    def get_tracking(self, carrier, tracking_number):
        self.calls += 1
        if self.inner is not None:
            return self.inner.get_tracking(carrier, tracking_number)
        return self.response


def _response(as_of, events=True):
    return CarrierTrackingResponse(
        carrier="SYN-CARRIER-2",
        tracking_number="SYN-TRK-0004",
        source="SYN-CARRIER-TRACKING-API",
        as_of=as_of,
        events=(CarrierEvent(as_of - timedelta(hours=1), "IN_TRANSIT", "SYN-HUB-CENTRAL"),) if events else (),
        exception_codes=(),
        estimated_delivery=None,
    )


def _forged_verified_record():
    """Every self-declared field says VERIFIED; only the attestation is guessed."""
    result = carrier_result("SYN-ORD-0004")
    record = st._evidence_from_carrier_result(result)
    record["attestation"] = "0" * 64
    return record


class EvidenceTrustTests(unittest.TestCase):
    """Step 3.2: structural validity is not proof of authenticity."""

    def test_structurally_valid_forgery_is_not_trusted(self):
        forged = _forged_verified_record()

        validate_evidence(forged)  # structure alone passes
        self.assertFalse(is_trusted_carrier_evidence(forged))

        state = initial_state("SYN-ORD-0004")
        state["carrier_evidence"] = forged
        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)
        with self.assertRaises(InvalidStateError):
            validate_state(state)  # VERIFIED claim in evidence, even with UNVERIFIED status

        state["delivery_status"] = DeliveryStatus.VERIFIED
        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_forged_evidence_cannot_back_a_verified_final_investigation(self):
        state = initial_state("SYN-ORD-0004")
        state["carrier_evidence"] = _forged_verified_record()
        state["final_investigation"] = investigation(status="VERIFIED")
        with self.assertRaises(InvalidStateError):
            validate_state(state)

    def test_attestation_from_a_genuine_record_does_not_transfer(self):
        genuine = carrier_evidence("SYN-ORD-0004")
        unverified = carrier_evidence("SYN-ORD-0006")
        edits = {
            "status_text": lambda r: r["payload"]["current_delivery_status"].update(status="DELIVERED"),
            "event_added": lambda r: r["payload"]["tracking_events"].append(dict(r["payload"]["tracking_events"][0])),
            "source": lambda r: r.update(source="SYN-OTHER-API"),
            "age": lambda r: r["freshness"].update(age_seconds=r["freshness"]["age_seconds"] + 1),
        }
        for name, edit in edits.items():
            with self.subTest(case=name):
                record = copy.deepcopy(genuine)
                edit(record)
                self.assertFalse(is_trusted_carrier_evidence(record))
                state = initial_state("SYN-ORD-0004")
                state["carrier_evidence"] = record
                state["delivery_status"] = DeliveryStatus.VERIFIED
                self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)

        # Upgrading genuinely UNVERIFIED evidence breaks its attestation.
        upgraded = {**copy.deepcopy(unverified), "verification": DeliveryStatus.VERIFIED}
        self.assertFalse(is_trusted_carrier_evidence(upgraded))

    def test_untrusted_paths_cannot_create_verified_or_attested_evidence(self):
        with self.assertRaises(TypeError):
            make_evidence(st.SOURCE_CARRIER, st.RETRIEVED, verification=DeliveryStatus.VERIFIED)
        with self.assertRaises(TypeError):
            make_evidence(st.SOURCE_CARRIER, st.RETRIEVED, attestation="0" * 64)
        self.assertFalse(hasattr(st, "evidence_from_carrier_result"))
        # A raw tool result (which could be fabricated) maps to an unattested record.
        self.assertIsNone(st._evidence_from_carrier_result(carrier_result("SYN-ORD-0004"))["attestation"])

    def test_retrieval_requires_trusted_runtime_context(self):
        for bad in [CONTEXT, None, {"access_context": CONTEXT}]:
            with self.subTest(value=type(bad).__name__), self.assertRaises(TypeError):
                retrieve_carrier_evidence("SYN-ORD-0004", runtime_context=bad)

    def test_denied_retrieval_raises_and_reads_nothing(self):
        contexts = {
            "no_user_permission": AccessContext(
                "SYN-USER-0001", "SYN-TENANT-A", frozenset({OMS_ORDER_READ}),
                frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
            ),
            "no_service_scope": AccessContext(
                "SYN-USER-0001", "SYN-TENANT-A", frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
                frozenset({OMS_READ_SCOPE}),
            ),
        }
        for name, context in contexts.items():
            adapter = _RecordingAdapter(inner=SyntheticCarrierAdapter(clock=lambda: NOW))
            with self.subTest(case=name), self.assertRaises(AccessDeniedError):
                retrieve_carrier_evidence(
                    "SYN-ORD-0004",
                    runtime_context=InvestigationRuntimeContext(access_context=context),
                    adapter=adapter,
                    clock=lambda: NOW,
                )
            self.assertEqual(adapter.calls, 0)

    def test_other_tenant_and_unknown_orders_yield_no_evidence(self):
        for order_id in ["SYN-ORD-0003", "SYN-ORD-9999"]:  # tenant B, absent
            with self.subTest(order_id=order_id), self.assertRaises(OrderNotFoundError):
                carrier_evidence(order_id)

    def test_trusted_unverified_evidence_is_attested_but_stays_unverified(self):
        record = carrier_evidence("SYN-ORD-0006")
        self.assertTrue(is_trusted_carrier_evidence(record))
        self.assertIs(record["verification"], DeliveryStatus.UNVERIFIED)

    def test_attestation_does_not_survive_a_new_process_key(self):
        state = verified_state()
        validate_state(state)
        with mock.patch.object(st, "_ATTESTATION_KEY", b"\x01" * 32):
            self.assertFalse(is_trusted_carrier_evidence(state["carrier_evidence"]))
            self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)
            with self.assertRaises(InvalidStateError):
                validate_state(state)

    def test_verification_must_be_enum_in_records(self):
        record = carrier_evidence("SYN-ORD-0006")
        record["verification"] = "UNVERIFIED"
        with self.assertRaises(InvalidStateError):
            validate_evidence(record)
        self.assertFalse(is_trusted_carrier_evidence(record))

    def test_trust_check_never_raises_on_garbage(self):
        for value in [None, "x", {}, [], {"attestation": "0" * 64}]:
            with self.subTest(value=repr(value)):
                self.assertFalse(is_trusted_carrier_evidence(value))


class FreshnessConsistencyTests(unittest.TestCase):
    """Step 3.2: contradictory freshness metadata is rejected, never repaired."""

    def _carrier(self, status, age, limit, *, as_of=None, retrieved=NOW):
        as_of = as_of if as_of is not None else (retrieved - timedelta(seconds=age or 0))
        return make_evidence(
            st.SOURCE_CARRIER,
            st.RETRIEVED,
            source="SYN-CARRIER-TRACKING-API",
            retrieved_at=retrieved.isoformat(),
            source_as_of=as_of.isoformat(),
            freshness_status=status,
            age_seconds=age,
            limit_seconds=limit,
        )

    def test_consistent_metadata_is_accepted(self):
        for status, age, limit in [
            ("FRESH", 0, 600), ("FRESH", 600, 600), ("STALE", 601, 600),
            ("UNKNOWN", None, 600), ("UNKNOWN", 30, None), ("UNKNOWN", None, None),
        ]:
            with self.subTest(status=status, age=age, limit=limit):
                self._carrier(status, age, limit)

    def test_contradictory_metadata_is_rejected(self):
        cases = {
            "fresh_over_limit": ("FRESH", 601, 600),
            "stale_within_limit": ("STALE", 600, 600),
            "stale_zero_age": ("STALE", 0, 600),
            "fresh_no_age": ("FRESH", None, 600),
            "fresh_no_limit": ("FRESH", 10, None),
            "stale_no_age": ("STALE", None, 600),
            "unknown_but_derivable": ("UNKNOWN", 10, 600),
        }
        for name, (status, age, limit) in cases.items():
            with self.subTest(case=name), self.assertRaises(InvalidStateError):
                self._carrier(status, age, limit)

    def test_age_must_match_timestamps(self):
        with self.assertRaises(InvalidStateError):
            self._carrier("FRESH", 60, 600, as_of=NOW - timedelta(hours=5))
        with self.assertRaises(InvalidStateError):  # as-of in the future
            self._carrier("FRESH", 0, 600, as_of=NOW + timedelta(minutes=5))

    def test_age_without_timestamps_is_not_accepted(self):
        for kwargs in [dict(retrieved_at=NOW.isoformat()), dict(source_as_of=NOW.isoformat()), {}]:
            with self.subTest(kwargs=sorted(kwargs)), self.assertRaises(InvalidStateError):
                make_evidence(st.SOURCE_CARRIER, st.RETRIEVED, age_seconds=0, **kwargs)

    def test_fresh_over_limit_verified_claim_is_rejected(self):
        record = carrier_evidence("SYN-ORD-0004")
        record["freshness"]["limit_seconds"] = record["freshness"]["age_seconds"] - 1
        with self.assertRaises(InvalidStateError):
            validate_evidence(record)
        state = initial_state("SYN-ORD-0004")
        state["carrier_evidence"] = record
        state["delivery_status"] = DeliveryStatus.VERIFIED
        self.assertIs(effective_delivery_status(state), DeliveryStatus.UNVERIFIED)

    def test_tool_result_with_contradictory_freshness_is_rejected(self):
        result = carrier_result("SYN-ORD-0004")
        result["freshness"]["limit_seconds"] = result["freshness"]["age_seconds"] - 1
        with self.assertRaises(InvalidStateError):
            st._evidence_from_carrier_result(result)

    def test_all_phase_2_carrier_outputs_are_preserved(self):
        for order_id in ["SYN-ORD-0001", "SYN-ORD-0002", "SYN-ORD-0004", "SYN-ORD-0005", "SYN-ORD-0006", "SYN-ORD-0007"]:
            with self.subTest(order_id=order_id):
                result = carrier_result(order_id)
                record = carrier_evidence(order_id)
                self.assertEqual(record["payload"], result)
                self.assertEqual(record["freshness"], result["freshness"])
                self.assertEqual(record["source_as_of"], result["source_as_of"])
                self.assertEqual(record["source"], result["source"])
                self.assertEqual(record["reason"], result["current_delivery_status"]["reason"])
                self.assertEqual(record["verification"].value, result["current_delivery_status"]["verification"])

    def test_edge_tool_outputs_are_preserved_without_invented_values(self):
        cases = {
            "as_of_in_future": (_response(NOW + timedelta(minutes=5)), "AS_OF_TIME_IN_FUTURE", "UNKNOWN"),
            "no_events": (_response(NOW - timedelta(minutes=5), events=False), "NO_TRACKING_EVENTS", "FRESH"),
            "at_limit": (_response(NOW - timedelta(hours=6)), None, "FRESH"),
            "just_over_limit": (_response(NOW - timedelta(hours=6, seconds=1)), "STALE_CARRIER_DATA", "STALE"),
            "malformed": ("not a response", "MALFORMED_CARRIER_RESPONSE", "UNKNOWN"),
        }
        for name, (response, reason, freshness) in cases.items():
            with self.subTest(case=name):
                record = carrier_evidence("SYN-ORD-0004", adapter=_RecordingAdapter(response=response))
                self.assertEqual(record["reason"], reason)
                self.assertEqual(record["freshness"]["status"], freshness)
                self.assertTrue(is_trusted_carrier_evidence(record))
                if freshness == "UNKNOWN":
                    self.assertIsNone(record["freshness"]["age_seconds"])
                if name == "malformed":
                    self.assertIsNone(record["source_as_of"])
                    self.assertIsNone(record["source"])


class ResumeFailClosedTests(unittest.TestCase):
    """Step 3.2: checkpointed VERIFIED evidence is not trusted after a resume.

    A new process is simulated by replacing the per-process key. Step 3.6
    will add the resume step; these tests pin the behavior it relies on.
    """

    NEW_PROCESS_KEY = b"\x02" * 32

    def _checkpoint(self):
        state = verified_state()
        state["final_investigation"] = investigation(status="VERIFIED")
        validate_state(state)
        return copy.deepcopy(state)  # stands in for a stored checkpoint

    def test_resumed_verified_state_fails_closed(self):
        checkpoint = self._checkpoint()
        with mock.patch.object(st, "_ATTESTATION_KEY", self.NEW_PROCESS_KEY):
            self.assertIs(effective_delivery_status(checkpoint), DeliveryStatus.UNVERIFIED)
            self.assertIs(checkpoint["requires_human_review"], True)
            with self.assertRaises(InvalidStateError):
                validate_state(checkpoint)

    def test_fresh_authorized_retrieval_restores_verified(self):
        checkpoint = self._checkpoint()
        with mock.patch.object(st, "_ATTESTATION_KEY", self.NEW_PROCESS_KEY):
            checkpoint["carrier_evidence"] = carrier_evidence("SYN-ORD-0004")
            validate_state(checkpoint)
            self.assertIs(effective_delivery_status(checkpoint), DeliveryStatus.VERIFIED)

    def test_failed_reauthorization_yields_no_evidence(self):
        denied = InvestigationRuntimeContext(
            access_context=AccessContext(
                "SYN-USER-0002", "SYN-TENANT-A", frozenset({OMS_ORDER_READ}), frozenset({OMS_READ_SCOPE})
            )
        )
        other_tenant = InvestigationRuntimeContext(
            access_context=AccessContext(
                "SYN-USER-0101", "SYN-TENANT-B",
                frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
                frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
            )
        )
        checkpoint = self._checkpoint()
        with mock.patch.object(st, "_ATTESTATION_KEY", self.NEW_PROCESS_KEY):
            for runtime, error in [(denied, AccessDeniedError), (other_tenant, OrderNotFoundError)]:
                with self.subTest(error=error.__name__), self.assertRaises(error):
                    retrieve_carrier_evidence("SYN-ORD-0004", runtime_context=runtime, clock=lambda: NOW)
            # The stale checkpointed evidence is still not trusted.
            self.assertIs(effective_delivery_status(checkpoint), DeliveryStatus.UNVERIFIED)

    def test_unavailable_carrier_on_refetch_stays_unverified(self):
        checkpoint = self._checkpoint()
        with mock.patch.object(st, "_ATTESTATION_KEY", self.NEW_PROCESS_KEY):
            checkpoint["carrier_evidence"] = carrier_evidence(
                "SYN-ORD-0004", adapter=_UnavailableAdapter()
            )
            self.assertIs(checkpoint["carrier_evidence"]["verification"], DeliveryStatus.UNVERIFIED)
            self.assertEqual(checkpoint["carrier_evidence"]["reason"], "CARRIER_UNAVAILABLE")
            self.assertIsNone(checkpoint["carrier_evidence"]["source_as_of"])
            self.assertIs(effective_delivery_status(checkpoint), DeliveryStatus.UNVERIFIED)
            self.assertIs(checkpoint["requires_human_review"], True)

    def test_key_and_runtime_context_never_enter_state(self):
        state = verified_state()
        dumped = json.dumps(
            {k: v for k, v in state.items() if k != "final_investigation"}, default=str
        )
        self.assertNotIn(st._ATTESTATION_KEY.hex(), dumped)
        for value in (CONTEXT.user_id, CONTEXT.tenant_id, *CONTEXT.user_permissions, *CONTEXT.service_scopes):
            self.assertNotIn(value, dumped)
        self.assertNotEqual(state["carrier_evidence"]["attestation"], st._ATTESTATION_KEY.hex())
        self.assertNotIn("attestation", state["carrier_evidence"]["payload"])


class _UnavailableAdapter:
    def get_tracking(self, carrier, tracking_number):
        raise CarrierUnavailableError("synthetic outage")


class ReducerAndErrorTests(unittest.TestCase):
    def test_merge_errors_appends_parallel_updates(self):
        a = make_safe_error("oms_retrieval", "OrderNotFound", source_system=st.SOURCE_OMS)
        b = make_safe_error("carrier_retrieval", "CARRIER_UNAVAILABLE", source_system="CARRIER", retryable=True)

        merged = merge_errors(merge_errors([], [a]), [b])

        self.assertEqual(merged, [a, b])
        self.assertEqual(merge_errors(merged, None), merged)

    def test_merge_errors_does_not_mutate_inputs(self):
        current = [make_safe_error("n1", "E1")]
        merge_errors(current, [make_safe_error("n2", "E2")])
        self.assertEqual(len(current), 1)

    def test_merge_errors_rejects_unsafe_items(self):
        for bad in [
            [{"node": "n", "error_code": "E", "source_system": None, "retryable": False, "message": "x"}],
            [{"node": "n", "error_code": "boom: password=x", "source_system": None, "retryable": False}],
            ["E1"],
            "E1",
        ]:
            with self.subTest(update=bad), self.assertRaises(InvalidStateError):
                merge_errors([], bad)

    def test_make_safe_error_rejects_free_text(self):
        with self.assertRaises(InvalidStateError):
            make_safe_error("node", "Traceback (most recent call last)")
        with self.assertRaises(InvalidStateError):
            make_safe_error("node", "E1", source_system="SNOWFLAKE")
        with self.assertRaises(InvalidStateError):
            make_safe_error("node", "E1", retryable="yes")

    def test_safe_error_has_no_message_field(self):
        self.assertEqual(set(st.SafeError.__annotations__), {"node", "error_code", "source_system", "retryable"})

    def test_keep_human_review_is_sticky(self):
        for current, update in [(True, False), (True, None), (False, False), (None, 0)]:
            with self.subTest(current=current, update=update):
                self.assertIs(keep_human_review(current, update), True)

    def test_state_with_invalid_error_item_is_rejected(self):
        state = initial_state("SYN-ORD-0001")
        state["errors"] = [{"node": "n", "error_code": "E"}]
        with self.assertRaises(InvalidStateError):
            validate_state(state)


class RuntimeContextTests(unittest.TestCase):
    def test_runtime_context_holds_access_context_outside_state(self):
        runtime = InvestigationRuntimeContext(access_context=CONTEXT)
        self.assertIs(runtime.access_context, CONTEXT)
        self.assertNotIn("access_context", InvestigationState.__annotations__)

    def test_runtime_context_requires_access_context(self):
        for bad in [None, {"user_id": "SYN-USER-0001"}, "token"]:
            with self.subTest(value=bad), self.assertRaises(TypeError):
                InvestigationRuntimeContext(access_context=bad)

    def test_runtime_context_is_immutable(self):
        runtime = InvestigationRuntimeContext(access_context=CONTEXT)
        with self.assertRaises(AttributeError):
            runtime.access_context = None


if __name__ == "__main__":
    unittest.main()
