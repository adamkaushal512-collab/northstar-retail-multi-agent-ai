"""Unit tests for the read-only get_carrier_tracking tool (Step 2.6C).

Synthetic data only. No AWS, Bedrock or network calls.
"""

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from northstar.integrations import synthetic_carrier
from northstar.integrations.synthetic_carrier import (
    SOURCE_NAME,
    CarrierEvent,
    CarrierUnavailableError,
    SyntheticCarrierAdapter,
)
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    DENIED_MESSAGE,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
)
from northstar.tools import carrier_tracking, registry
from northstar.tools.carrier_tracking import (
    SYNTHETIC_FRESHNESS_LIMIT,
    TOOL_SPEC,
    get_carrier_tracking,
    run_tool,
)
from northstar.tools.order_details import InvalidToolInputError, OrderNotFoundError
from northstar.tools.registry import UnsupportedToolError, run_named_tool

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ALL_PERMS = frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ})
ALL_SCOPES = frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE})
TENANT_A = AccessContext("SYN-USER-0001", "SYN-TENANT-A", ALL_PERMS, ALL_SCOPES)
TENANT_B = AccessContext("SYN-USER-0101", "SYN-TENANT-B", ALL_PERMS, ALL_SCOPES)


def clock():
    return NOW


def track(order_id, context=TENANT_A, **kwargs):
    kwargs.setdefault("clock", clock)
    return get_carrier_tracking(order_id, access_context=context, **kwargs)


class FakeAdapter:
    """Returns a fixed value, to inject malformed or edge-case responses."""

    def __init__(self, value):
        self.value = value

    def get_tracking(self, carrier, tracking_number):
        return self.value


def good_response(**changes):
    response = SyntheticCarrierAdapter(clock).get_tracking("SYN-CARRIER-2", "SYN-TRK-0004")
    return replace(response, **changes)


class ToolSpecTests(unittest.TestCase):
    def test_spec_shape(self):
        spec = TOOL_SPEC["toolSpec"]
        self.assertEqual(spec["name"], "get_carrier_tracking")
        schema = spec["inputSchema"]["json"]
        self.assertEqual(schema["required"], ["order_id"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(list(schema["properties"]), ["order_id"])
        json.dumps(TOOL_SPEC)

    def test_schema_exposes_no_access_fields(self):
        text = json.dumps(TOOL_SPEC["toolSpec"]["inputSchema"]).lower()
        for word in ("tenant", "customer", "user_id", "permission", "tracking", "carrier"):
            self.assertNotIn(word, text)


class SuccessTests(unittest.TestCase):
    def test_normal_tracking_is_verified(self):
        result = track("SYN-ORD-0004")
        status = result["current_delivery_status"]
        self.assertEqual(status["verification"], "VERIFIED")
        self.assertEqual(status["status"], "OUT_FOR_DELIVERY")
        self.assertIsNone(status["reason"])
        self.assertEqual(status["carrier_estimated_delivery"], "2026-11-10")
        self.assertEqual(result["carrier"], "SYN-CARRIER-2")
        self.assertEqual(result["tracking_number"], "SYN-TRK-0004")
        self.assertEqual(result["source"], SOURCE_NAME)
        self.assertEqual(result["retrieved_at"], NOW.isoformat())
        self.assertEqual(result["source_as_of"], (NOW - timedelta(minutes=15)).isoformat())
        self.assertEqual(result["freshness"], {"status": "FRESH", "age_seconds": 900, "limit_seconds": 21600})

    def test_events_are_historical_with_timestamps_and_source(self):
        events = track("SYN-ORD-0004")["tracking_events"]
        self.assertEqual([e["event_code"] for e in events], ["PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY"])
        times = [e["event_time"] for e in events]
        self.assertEqual(times, sorted(times))
        for event in events:
            self.assertEqual(event["evidence_type"], "HISTORICAL_CARRIER_EVENT")
            self.assertEqual(event["source"], SOURCE_NAME)
            datetime.fromisoformat(event["event_time"])

    def test_weather_exception(self):
        result = track("SYN-ORD-0001")
        self.assertEqual(result["carrier_exception_codes"], ["WEATHER_DELAY"])
        self.assertEqual(result["current_delivery_status"]["verification"], "VERIFIED")
        self.assertEqual(result["current_delivery_status"]["status"], "EXCEPTION")
        # The carrier gave no estimate, so none is invented.
        self.assertIsNone(result["current_delivery_status"]["carrier_estimated_delivery"])

    def test_run_tool_and_registry(self):
        for result in (
            run_tool({"order_id": "SYN-ORD-0004"}, access_context=TENANT_A),
            run_named_tool("get_carrier_tracking", {"order_id": "SYN-ORD-0004"}, access_context=TENANT_A),
        ):
            self.assertEqual(result["order_id"], "SYN-ORD-0004")
            self.assertNotIn("customer_id", json.dumps(result))
            self.assertNotIn("tenant", json.dumps(result).lower())

    def test_tenant_b_reads_its_own_order(self):
        result = track("SYN-ORD-0003", TENANT_B)
        self.assertEqual(result["current_delivery_status"]["verification"], "VERIFIED")


class FreshnessTests(unittest.TestCase):
    def assert_unverified(self, result, reason):
        status = result["current_delivery_status"]
        self.assertEqual(status["verification"], "UNVERIFIED")
        self.assertEqual(status["status"], "UNKNOWN")
        self.assertEqual(status["reason"], reason)
        self.assertIsNone(status["carrier_estimated_delivery"])

    def test_stale_tracking_is_unverified_but_keeps_history(self):
        result = track("SYN-ORD-0005")
        self.assert_unverified(result, "STALE_CARRIER_DATA")
        self.assertEqual(result["freshness"]["status"], "STALE")
        self.assertEqual(result["freshness"]["age_seconds"], 3 * 86400)
        self.assertEqual(result["source_as_of"], (NOW - timedelta(days=3)).isoformat())
        self.assertEqual([e["event_code"] for e in result["tracking_events"]], ["PICKED_UP", "IN_TRANSIT"])

    def test_limit_is_configurable_and_boundary_is_fresh(self):
        # SYN-TRK-0004 as-of age is exactly 15 minutes.
        at_limit = track("SYN-ORD-0004", freshness_limit=timedelta(minutes=15))
        self.assertEqual(at_limit["freshness"]["status"], "FRESH")
        over = track("SYN-ORD-0004", freshness_limit=timedelta(minutes=14))
        self.assert_unverified(over, "STALE_CARRIER_DATA")

    def test_default_limit_is_placeholder(self):
        self.assertEqual(SYNTHETIC_FRESHNESS_LIMIT, timedelta(hours=6))

    def test_as_of_in_future_is_unverified(self):
        adapter = FakeAdapter(good_response(as_of=NOW + timedelta(hours=1)))
        result = track("SYN-ORD-0004", adapter=adapter)
        self.assert_unverified(result, "AS_OF_TIME_IN_FUTURE")
        self.assertEqual(result["freshness"]["status"], "UNKNOWN")

    def test_real_clock_scenarios_stay_deterministic(self):
        result = get_carrier_tracking("SYN-ORD-0004", access_context=TENANT_A)
        self.assertEqual(result["current_delivery_status"]["verification"], "VERIFIED")


class MissingDataTests(unittest.TestCase):
    assert_unverified = FreshnessTests.assert_unverified

    def test_no_tracking_number_in_oms(self):
        with mock.patch.object(SyntheticCarrierAdapter, "get_tracking") as get:
            result = track("SYN-ORD-0002")
            get.assert_not_called()
        self.assert_unverified(result, "NO_TRACKING_NUMBER_IN_OMS")
        self.assertIsNone(result["tracking_number"])
        self.assertEqual(result["tracking_events"], [])

    def test_carrier_has_no_record(self):
        result = track("SYN-ORD-0007")
        self.assert_unverified(result, "CARRIER_RECORD_NOT_FOUND")
        self.assertIsNone(result["source"])
        self.assertIsNone(result["source_as_of"])
        self.assertEqual(result["tracking_events"], [])

    def test_no_events_is_unverified(self):
        result = track("SYN-ORD-0004", adapter=FakeAdapter(good_response(events=())))
        self.assert_unverified(result, "NO_TRACKING_EVENTS")

    def test_carrier_mismatch_in_adapter_returns_none(self):
        self.assertIsNone(SyntheticCarrierAdapter(clock).get_tracking("SYN-CARRIER-1", "SYN-TRK-0004"))


class CarrierFailureTests(unittest.TestCase):
    assert_unverified = FreshnessTests.assert_unverified

    def test_carrier_unavailable(self):
        result = track("SYN-ORD-0006")
        self.assert_unverified(result, "CARRIER_UNAVAILABLE")
        self.assertEqual(result["tracking_events"], [])
        self.assertEqual(result["carrier_exception_codes"], [])
        self.assertIsNone(result["source_as_of"])
        self.assertEqual(result["freshness"]["status"], "UNKNOWN")

    def test_adapter_raises_unavailable(self):
        with self.assertRaises(CarrierUnavailableError):
            SyntheticCarrierAdapter(clock).get_tracking("SYN-CARRIER-2", "SYN-TRK-0006")

    def test_malformed_responses(self):
        naive = NOW.replace(tzinfo=None)
        bad_values = {
            "not a response": {"status": "DELIVERED"},
            "wrong tracking number": good_response(tracking_number="SYN-TRK-0001"),
            "wrong carrier": good_response(carrier="SYN-CARRIER-1"),
            "naive as_of": good_response(as_of=naive),
            "events not tuple": good_response(events=[]),
            "bad event": good_response(events=({"event_code": "DELIVERED"},)),
            "event after as_of": good_response(
                events=(CarrierEvent(NOW + timedelta(days=1), "DELIVERED", "SYN-DEPOT-EAST"),)
            ),
            "bad estimate": good_response(estimated_delivery=20261110),
        }
        for label, value in bad_values.items():
            with self.subTest(label):
                result = track("SYN-ORD-0004", adapter=FakeAdapter(value))
                self.assert_unverified(result, "MALFORMED_CARRIER_RESPONSE")
                self.assertEqual(result["tracking_events"], [])

    def test_no_snowflake_or_cache_substitution(self):
        """A previous good result is not reused when the carrier is down (TS-N04)."""
        track("SYN-ORD-0004")
        down = mock.Mock()
        down.get_tracking.side_effect = CarrierUnavailableError()
        result = track("SYN-ORD-0004", adapter=down)
        self.assert_unverified(result, "CARRIER_UNAVAILABLE")


class InvalidInputTests(unittest.TestCase):
    def test_invalid_inputs(self):
        for tool_input in (None, "SYN-ORD-0001", {}, {"order_id": 1}, {"order_id": "SYN-ORD-1"}):
            with self.subTest(tool_input=tool_input), self.assertRaises(InvalidToolInputError):
                run_tool(tool_input, access_context=TENANT_A)

    def test_model_cannot_supply_access_or_tracking_fields(self):
        extras = (
            {"tenant_id": "SYN-TENANT-B"},
            {"customer_id": "SYN-CUST-0101"},
            {"tracking_number": "SYN-TRK-0003"},
            {"carrier": "SYN-CARRIER-1"},
        )
        for extra in extras:
            with self.subTest(extra=extra), self.assertRaises(InvalidToolInputError):
                run_tool({"order_id": "SYN-ORD-0003", **extra}, access_context=TENANT_A)


class UnknownOrderTests(unittest.TestCase):
    def test_unknown_and_other_tenant_look_the_same(self):
        with mock.patch.object(SyntheticCarrierAdapter, "get_tracking") as get:
            with self.assertRaises(OrderNotFoundError) as other:
                track("SYN-ORD-0003", TENANT_A)
            with self.assertRaises(OrderNotFoundError) as unknown:
                track("SYN-ORD-9999", TENANT_A)
            get.assert_not_called()
        self.assertEqual(str(other.exception), str(unknown.exception))


class AuthorizationTests(unittest.TestCase):
    def assert_denied(self, context, check):
        with mock.patch.object(carrier_tracking, "find_order") as find, mock.patch.object(
            SyntheticCarrierAdapter, "get_tracking"
        ) as get:
            with self.assertRaises(AccessDeniedError) as ctx:
                run_tool({"order_id": "SYN-ORD-0004"}, access_context=context)
            find.assert_not_called()
            get.assert_not_called()
        self.assertEqual(ctx.exception.check, check)
        self.assertEqual(str(ctx.exception), DENIED_MESSAGE)

    def test_no_or_wrong_context(self):
        self.assert_denied(None, "context")
        self.assert_denied({"tenant_id": "SYN-TENANT-A"}, "context")

    def test_each_permission_and_scope_required(self):
        cases = (
            (ALL_PERMS - {OMS_ORDER_READ}, ALL_SCOPES, "user"),
            (ALL_PERMS - {CARRIER_TRACKING_READ}, ALL_SCOPES, "user"),
            (ALL_PERMS, ALL_SCOPES - {OMS_READ_SCOPE}, "service"),
            (ALL_PERMS, ALL_SCOPES - {CARRIER_READ_SCOPE}, "service"),
        )
        for perms, scopes, check in cases:
            with self.subTest(perms=perms, scopes=scopes):
                self.assert_denied(AccessContext("SYN-USER-0001", "SYN-TENANT-A", perms, scopes), check)

    def test_denied_before_input_validation(self):
        with self.assertRaises(AccessDeniedError):
            run_tool({"order_id": 123}, access_context=None)


class RegistryTests(unittest.TestCase):
    def test_unsupported_tool_names(self):
        for name in ("refund_order", "get_carrier_tracking ", "GET_CARRIER_TRACKING", "", None, 1):
            with self.subTest(name=name), self.assertRaises(UnsupportedToolError):
                run_named_tool(name, {"order_id": "SYN-ORD-0004"}, access_context=TENANT_A)

    def test_specs_match_runners(self):
        names = [spec["toolSpec"]["name"] for spec in registry.TOOL_SPECS]
        self.assertEqual(names, ["get_order_details", "get_carrier_tracking"])
        self.assertEqual(sorted(names), sorted(registry._RUNNERS))
        json.dumps(registry.TOOL_SPECS)


class ReadOnlyTests(unittest.TestCase):
    def test_no_write_or_contact_functions_exposed(self):
        for module in (carrier_tracking, synthetic_carrier, registry):
            public = {n for n in dir(module) if not n.startswith("_")}
            for word in ("refund", "reship", "cancel", "update", "create", "delete", "claim", "send", "notify"):
                with self.subTest(module=module.__name__, word=word):
                    self.assertFalse([n for n in public if word in n.lower()])

    def test_results_are_copies(self):
        first = track("SYN-ORD-0004")
        first["tracking_events"].clear()
        first["current_delivery_status"]["verification"] = "VERIFIED-BY-MODEL"
        again = track("SYN-ORD-0004")
        self.assertEqual(len(again["tracking_events"]), 3)
        self.assertEqual(again["current_delivery_status"]["verification"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
