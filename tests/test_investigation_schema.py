"""Unit tests for the delayed-delivery investigation schema (Step 2.5B).

Synthetic data only. No AWS or Bedrock calls. Requires pydantic (v2).
"""

import copy
import json
import unittest

from pydantic import ValidationError

from northstar.evidence.investigation_schema import (
    DelayedDeliveryInvestigation,
    DeliveryStatus,
    EvidenceClassification,
)

VALID = {
    "order_id": "SYN-ORD-0001",
    "delivery_status": "UNVERIFIED",
    "summary": "Weather delay reported in the provided evidence.",
    "likely_cause": "Weather delay at regional hub (reported in the provided evidence).",
    "evidence": [
        {
            "classification": "Confirmed",
            "fact": "Order SYN-ORD-0001 exists.",
            "source": "[SYNTHETIC] OMS snapshot",
        },
        {
            "classification": "Historical",
            "fact": "Carrier scan reported a weather exception.",
            "source": "[SYNTHETIC] carrier scan event",
        },
        {
            "classification": "Unknown",
            "fact": "Current carrier status.",
            "source": "none",
        },
    ],
    "missing_information": ["Current carrier tracking status"],
    "recommended_next_steps": ["CS agent verifies tracking with the carrier"],
    "requires_human_review": True,
}


def as_json(payload):
    return json.dumps(payload)


def with_changes(**changes):
    payload = copy.deepcopy(VALID)
    payload.update(changes)
    return payload


class ValidInvestigationTests(unittest.TestCase):
    def test_valid_json_parses(self):
        result = DelayedDeliveryInvestigation.model_validate_json(as_json(VALID))

        self.assertEqual(result.order_id, "SYN-ORD-0001")
        self.assertIs(result.delivery_status, DeliveryStatus.UNVERIFIED)
        self.assertEqual(len(result.evidence), 3)
        self.assertIs(result.evidence[1].classification, EvidenceClassification.HISTORICAL)
        self.assertTrue(result.requires_human_review)

    def test_requires_human_review_defaults_to_true(self):
        payload = copy.deepcopy(VALID)
        del payload["requires_human_review"]

        result = DelayedDeliveryInvestigation.model_validate_json(as_json(payload))

        self.assertTrue(result.requires_human_review)

    def test_likely_cause_may_be_null(self):
        result = DelayedDeliveryInvestigation.model_validate_json(
            as_json(with_changes(likely_cause=None))
        )
        self.assertIsNone(result.likely_cause)

    def test_empty_lists_are_allowed(self):
        result = DelayedDeliveryInvestigation.model_validate_json(
            as_json(with_changes(evidence=[], missing_information=[], recommended_next_steps=[]))
        )
        self.assertEqual(result.evidence, [])

    def test_verified_status_parses(self):
        result = DelayedDeliveryInvestigation.model_validate_json(
            as_json(with_changes(delivery_status="VERIFIED"))
        )
        self.assertIs(result.delivery_status, DeliveryStatus.VERIFIED)

    def test_round_trip(self):
        result = DelayedDeliveryInvestigation.model_validate_json(as_json(VALID))
        again = DelayedDeliveryInvestigation.model_validate_json(result.model_dump_json())
        self.assertEqual(result, again)


class InvalidInvestigationTests(unittest.TestCase):
    def assertInvalid(self, text):
        with self.assertRaises(ValidationError):
            DelayedDeliveryInvestigation.model_validate_json(text)

    def test_malformed_json(self):
        self.assertInvalid('{"order_id": "SYN-ORD-0001",')

    def test_non_object_json(self):
        self.assertInvalid(as_json([VALID]))

    def test_missing_required_fields(self):
        for field in (
            "order_id",
            "delivery_status",
            "summary",
            "likely_cause",
            "evidence",
            "missing_information",
            "recommended_next_steps",
        ):
            payload = copy.deepcopy(VALID)
            del payload[field]
            with self.subTest(field=field):
                self.assertInvalid(as_json(payload))

    def test_unexpected_top_level_field(self):
        self.assertInvalid(as_json(with_changes(refund_approved=True)))

    def test_unexpected_evidence_field(self):
        payload = copy.deepcopy(VALID)
        payload["evidence"][0]["confidence"] = 0.9
        self.assertInvalid(as_json(payload))

    def test_empty_or_blank_order_id(self):
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=repr(value)):
                self.assertInvalid(as_json(with_changes(order_id=value)))

    def test_empty_or_blank_evidence_fact(self):
        for value in ("", "  "):
            payload = copy.deepcopy(VALID)
            payload["evidence"][0]["fact"] = value
            with self.subTest(value=repr(value)):
                self.assertInvalid(as_json(payload))

    def test_invalid_delivery_status(self):
        for value in ("verified", "IN_TRANSIT", "DELIVERED", ""):
            with self.subTest(value=value):
                self.assertInvalid(as_json(with_changes(delivery_status=value)))

    def test_invalid_evidence_classification(self):
        for value in ("confirmed", "Verified", "(Confirmed)", ""):
            payload = copy.deepcopy(VALID)
            payload["evidence"][0]["classification"] = value
            with self.subTest(value=value):
                self.assertInvalid(as_json(payload))

    def test_strict_types_reject_coercion(self):
        cases = {
            "order_id as number": with_changes(order_id=1001),
            "summary as null": with_changes(summary=None),
            "likely_cause as number": with_changes(likely_cause=5),
            "requires_human_review as string": with_changes(requires_human_review="true"),
            "requires_human_review as int": with_changes(requires_human_review=1),
            "evidence as object": with_changes(evidence=VALID["evidence"][0]),
            "missing_information as string": with_changes(missing_information="tracking"),
            "next steps item as number": with_changes(recommended_next_steps=[1]),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.assertInvalid(as_json(payload))


class RequiresHumanReviewTests(unittest.TestCase):
    """Regression: requires_human_review is Literal[True] [Decision D18]."""

    def test_omitted_defaults_to_true(self):
        payload = copy.deepcopy(VALID)
        del payload["requires_human_review"]

        result = DelayedDeliveryInvestigation.model_validate_json(as_json(payload))

        self.assertIs(result.requires_human_review, True)

    def test_explicit_true_accepted(self):
        result = DelayedDeliveryInvestigation.model_validate_json(
            as_json(with_changes(requires_human_review=True))
        )
        self.assertIs(result.requires_human_review, True)

    def test_explicit_false_rejected(self):
        with self.assertRaises(ValidationError):
            DelayedDeliveryInvestigation.model_validate_json(
                as_json(with_changes(requires_human_review=False))
            )

    def test_string_true_rejected(self):
        with self.assertRaises(ValidationError):
            DelayedDeliveryInvestigation.model_validate_json(
                as_json(with_changes(requires_human_review="true"))
            )

    def test_python_false_rejected(self):
        payload = with_changes(requires_human_review=False)
        with self.assertRaises(ValidationError):
            DelayedDeliveryInvestigation.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
