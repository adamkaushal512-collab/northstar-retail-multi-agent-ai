"""Structured schema for a delayed-delivery investigation draft (Step 2.5B).

[Proposal] Output contract for model-drafted investigations. It is provider
neutral: it does not import or depend on any LLM adapter (for example
``northstar.llm.bedrock_converse``), so alternatives A, B and C can all be
checked against the same shape (AA sections 2 and 8). Nothing here selects
an architecture, an agent count or a technology (ADR-001 to ADR-005 remain
Proposed).

Validation is strict and closed:

- ``extra="forbid"``: unexpected fields are rejected.
- ``strict=True``: no type coercion (for example ``"true"`` is not a bool,
  ``123`` is not a string). Validate model output with
  ``DelayedDeliveryInvestigation.model_validate_json(text)``; in JSON mode
  enum fields accept their string values.
- ``order_id`` and every evidence ``fact`` must contain at least one
  non-whitespace character.

The schema checks shape only. It does not verify that facts are true, that
sources exist, or that a status is current [Decision D12, D15]. The
platform is never an approver [Decision D18]; ``requires_human_review``
defaults to true and only ``true`` is accepted (``false`` is rejected).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints

# At least one non-whitespace character; the value itself is not modified.
NonBlankStr = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


def _require_bool(value: object) -> object:
    # Literal[True] alone accepts 1 and 1.0 (1 == True), even in strict mode.
    if type(value) is not bool:
        raise ValueError("must be a boolean")
    return value


# Only the boolean true is accepted [Decision D18].
AlwaysTrue = Annotated[Literal[True], BeforeValidator(_require_bool)]


class DeliveryStatus(str, Enum):
    """Whether the current delivery status is confirmed by a current source."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class EvidenceClassification(str, Enum):
    """Labels matching the adapter prompt's (Confirmed)/(Historical)/(Unknown)."""

    CONFIRMED = "Confirmed"
    HISTORICAL = "Historical"
    UNKNOWN = "Unknown"


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class EvidenceItem(_StrictModel):
    """One labeled fact and the source it was drawn from."""

    classification: EvidenceClassification
    fact: NonBlankStr
    source: str


class DelayedDeliveryInvestigation(_StrictModel):
    """Draft investigation result for one delayed order."""

    order_id: NonBlankStr
    delivery_status: DeliveryStatus
    summary: str
    likely_cause: str | None
    evidence: list[EvidenceItem]
    missing_information: list[str]
    recommended_next_steps: list[str]
    requires_human_review: AlwaysTrue = True
