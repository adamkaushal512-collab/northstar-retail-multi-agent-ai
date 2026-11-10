"""``get_carrier_tracking``: read-only synthetic carrier tracking lookup (Step 2.6C).

[Proposal] The Converse ``toolSpec`` shape follows ADR-001 (Proposed). The
model may send only ``order_id``. The tenant, carrier and tracking number
come from trusted OMS data resolved under the ``AccessContext`` tenant, never
from model input [Decision D16; FR-AUTH-02].

Verification rules [Decision D15; FR-EVD-03, FR-EVD-04, FR-REC-02]:
- Carrier events are always returned as historical evidence with their
  timestamps; they are never by themselves the verified current status.
- The current delivery status is VERIFIED only when the carrier responded,
  the response is well-formed, it has at least one event and its as-of time
  is within the freshness limit. Otherwise it is UNVERIFIED with status
  UNKNOWN and a machine-readable reason.
- No tracking event, delivery date, carrier confirmation or SLA commitment is
  ever generated. The carrier's own estimate is passed through only when the
  status is VERIFIED; it is a carrier-reported value, not a commitment.
- No Snowflake or cache substitution when the carrier is unavailable.
- Step 2.6E: any unexpected adapter exception is also UNVERIFIED
  (``CARRIER_ADAPTER_ERROR``); its message is not returned.

No refunds, reships, carrier claims, messages or other actions (FR-RO-04).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from northstar.integrations.synthetic_carrier import (
    CarrierEvent,
    CarrierTrackingResponse,
    CarrierTrackingSource,
    CarrierUnavailableError,
    Clock,
    SyntheticCarrierAdapter,
    utc_now,
)
from northstar.integrations.synthetic_oms import find_order
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    require_read,
)
from northstar.tools.order_details import INPUT_SCHEMA, OrderNotFoundError, validate_tool_input

TOOL_NAME = "get_carrier_tracking"

TOOL_SPEC: dict[str, Any] = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": (
            "Read-only lookup of carrier tracking for the shipment recorded on one order. "
            "Returns historical carrier events with timestamps, the data source, retrieval "
            "time, freshness, and whether the current delivery status is verified. An "
            "UNVERIFIED status means the current delivery status is unknown. Performs no "
            "actions and contacts no one."
        ),
        "inputSchema": {"json": INPUT_SCHEMA},
    }
}

# [Assumption] Synthetic placeholder for tests only. Real per-domain freshness
# limits are configuration and their values are [Open] (FR-EVD-04 AC2, WF Q3).
SYNTHETIC_FRESHNESS_LIMIT = timedelta(hours=6)

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
UNKNOWN_STATUS = "UNKNOWN"

FRESH = "FRESH"
STALE = "STALE"
FRESHNESS_UNKNOWN = "UNKNOWN"

EVENT_EVIDENCE_TYPE = "HISTORICAL_CARRIER_EVENT"

# Reasons a current delivery status is UNVERIFIED.
REASON_NO_TRACKING_NUMBER = "NO_TRACKING_NUMBER_IN_OMS"
REASON_NOT_FOUND = "CARRIER_RECORD_NOT_FOUND"
REASON_UNAVAILABLE = "CARRIER_UNAVAILABLE"
# Step 2.6E: the adapter raised something other than CarrierUnavailableError.
REASON_ADAPTER_ERROR = "CARRIER_ADAPTER_ERROR"
REASON_MALFORMED = "MALFORMED_CARRIER_RESPONSE"
REASON_NO_EVENTS = "NO_TRACKING_EVENTS"
REASON_STALE = "STALE_CARRIER_DATA"
REASON_AS_OF_IN_FUTURE = "AS_OF_TIME_IN_FUTURE"


def _authorize(access_context: object) -> AccessContext:
    """Both reads (OMS to resolve the order, carrier for tracking) must be allowed."""
    context = require_read(access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
    return require_read(context, CARRIER_TRACKING_READ, CARRIER_READ_SCOPE)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _is_aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _well_formed(response: object, carrier: str, tracking_number: str) -> bool:
    """Treat anything unexpected as malformed, i.e. unavailable (TS-N07)."""
    if not isinstance(response, CarrierTrackingResponse):
        return False
    if response.carrier != carrier or response.tracking_number != tracking_number:
        return False
    if not (isinstance(response.source, str) and response.source and _is_aware(response.as_of)):
        return False
    if not isinstance(response.events, tuple) or not isinstance(response.exception_codes, tuple):
        return False
    if not all(isinstance(code, str) for code in response.exception_codes):
        return False
    if response.estimated_delivery is not None and not isinstance(response.estimated_delivery, str):
        return False
    for event in response.events:
        if not (isinstance(event, CarrierEvent) and _is_aware(event.event_time)):
            return False
        if not (isinstance(event.event_code, str) and isinstance(event.location, str)):
            return False
        if event.event_time > response.as_of:
            return False
    return True


def _result(
    record,
    *,
    retrieved_at: datetime,
    limit: timedelta,
    response: CarrierTrackingResponse | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    freshness: dict[str, Any] = {
        "status": FRESHNESS_UNKNOWN,
        "age_seconds": None,
        "limit_seconds": int(limit.total_seconds()),
    }
    events: list[dict[str, str]] = []
    exception_codes: list[str] = []

    if response is not None:
        age = retrieved_at - response.as_of
        events = [
            {
                "evidence_type": EVENT_EVIDENCE_TYPE,
                "event_time": _iso(e.event_time),
                "event_code": e.event_code,
                "location": e.location,
                "source": response.source,
            }
            for e in sorted(response.events, key=lambda e: e.event_time)
        ]
        exception_codes = list(response.exception_codes)
        if age < timedelta(0):
            reason = REASON_AS_OF_IN_FUTURE
        else:
            freshness["age_seconds"] = int(age.total_seconds())
            freshness["status"] = STALE if age > limit else FRESH
            if age > limit:
                reason = REASON_STALE
            elif not events:
                reason = REASON_NO_EVENTS

    verified = response is not None and reason is None
    return {
        "order_id": record.order_id,
        "carrier": record.carrier,
        "tracking_number": record.tracking_number,
        "source": response.source if response is not None else None,
        "retrieved_at": _iso(retrieved_at),
        "source_as_of": _iso(response.as_of) if response is not None else None,
        "freshness": freshness,
        "current_delivery_status": {
            "verification": VERIFIED if verified else UNVERIFIED,
            "status": events[-1]["event_code"] if verified else UNKNOWN_STATUS,
            "reason": reason,
            "carrier_estimated_delivery": response.estimated_delivery if verified else None,
        },
        "tracking_events": events,
        "carrier_exception_codes": exception_codes,
    }


def get_carrier_tracking(
    order_id: object,
    *,
    access_context: object,
    adapter: CarrierTrackingSource | None = None,
    clock: Clock = utc_now,
    freshness_limit: timedelta = SYNTHETIC_FRESHNESS_LIMIT,
) -> dict[str, Any]:
    """Return carrier tracking evidence for an authorized, known order.

    ``adapter``, ``clock`` and ``freshness_limit`` are supplied by trusted
    code only; the model controls ``order_id`` alone.
    """
    context = _authorize(access_context)
    validated_id = validate_tool_input({"order_id": order_id})
    record = find_order(validated_id, context.tenant_id)
    if record is None:
        raise OrderNotFoundError("order not found")

    def done(**kwargs: Any) -> dict[str, Any]:
        return _result(record, retrieved_at=clock(), limit=freshness_limit, **kwargs)

    if not record.tracking_number:
        return done(reason=REASON_NO_TRACKING_NUMBER)

    source = adapter if adapter is not None else SyntheticCarrierAdapter(clock=clock)
    try:
        response = source.get_tracking(record.carrier, record.tracking_number)
    except CarrierUnavailableError:
        return done(reason=REASON_UNAVAILABLE)
    except Exception:  # noqa: BLE001 - any adapter fault means status unknown [Decision D15]
        # Step 2.6E: an unexpected adapter exception is treated as unavailable,
        # never as a verified or cached status. The exception is not returned.
        return done(reason=REASON_ADAPTER_ERROR)
    if response is None:
        return done(reason=REASON_NOT_FOUND)
    if not _well_formed(response, record.carrier, record.tracking_number):
        return done(reason=REASON_MALFORMED)
    return done(response=response)


def run_tool(tool_input: object, *, access_context: object) -> dict[str, Any]:
    """Entry point for a future tool loop: model arguments and context stay separate."""
    _authorize(access_context)
    order_id = validate_tool_input(tool_input)
    return get_carrier_tracking(order_id, access_context=access_context)
