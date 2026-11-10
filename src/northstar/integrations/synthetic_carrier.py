"""Synthetic, in-memory Shipping/Carrier tracking adapter (Step 2.6C).

[SYNTHETIC] Every record is invented for a fictional company. No external
API calls, no network, no credentials. Read-only [Decision D11]: there is no
create, update, claim or message path (FR-RO-04).

The adapter only reports what the synthetic carrier "returns". It does not
decide whether a status is verified; that rule lives in the tool, so a real
adapter can replace this one without changing verification behavior.

Timestamps are generated relative to an injectable clock so scenarios stay
deterministic (fresh stays fresh, stale stays stale) whatever the real date.

Simulated scenarios, keyed by (carrier, tracking number):
- SYN-TRK-0001: weather exception (TS-P01)
- SYN-TRK-0003: normal tracking, tenant B order
- SYN-TRK-0004: normal tracking, out for delivery, carrier estimate given
- SYN-TRK-0005: stale tracking, as-of time days old (TS-N05)
- SYN-TRK-0006: carrier unavailable (TS-P07, TS-N04)
- SYN-TRK-0007: absent; carrier has no record for the tracking number
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

SOURCE_NAME = "SYN-CARRIER-TRACKING-API"

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CarrierUnavailableError(RuntimeError):
    """The carrier source could not be reached; status is unknown [Decision D15]."""


@dataclass(frozen=True)
class CarrierEvent:
    """One carrier-reported scan or exception. Historical, not current status."""

    event_time: datetime  # carrier's event time, timezone-aware
    event_code: str
    location: str


@dataclass(frozen=True)
class CarrierTrackingResponse:
    carrier: str
    tracking_number: str
    source: str
    as_of: datetime  # carrier's own as-of time, timezone-aware
    events: tuple[CarrierEvent, ...]
    exception_codes: tuple[str, ...]
    estimated_delivery: str | None  # carrier-reported ISO date, or None if not given


class CarrierTrackingSource(Protocol):
    """Read-only interface any carrier adapter (synthetic or real) implements.

    Returns None when the carrier has no record for the tracking number.
    Raises ``CarrierUnavailableError`` when the source cannot be reached.
    """

    def get_tracking(self, carrier: str, tracking_number: str) -> CarrierTrackingResponse | None: ...


@dataclass(frozen=True)
class _Scenario:
    carrier: str
    as_of_age: timedelta
    events: tuple[tuple[timedelta, str, str], ...] = ()  # (age, event_code, location)
    exception_codes: tuple[str, ...] = ()
    estimated_delivery_in: timedelta | None = None
    unavailable: bool = False


_H = timedelta(hours=1)
_D = timedelta(days=1)

_SCENARIOS: Mapping[str, _Scenario] = MappingProxyType(
    {
        "SYN-TRK-0001": _Scenario(
            carrier="SYN-CARRIER-1",
            as_of_age=timedelta(minutes=30),
            events=(
                (6 * _D, "PICKED_UP", "SYN-HUB-NORTH"),
                (5 * _D, "IN_TRANSIT", "SYN-HUB-CENTRAL"),
                (4 * _D, "EXCEPTION", "SYN-HUB-CENTRAL"),
            ),
            exception_codes=("WEATHER_DELAY",),
        ),
        "SYN-TRK-0003": _Scenario(
            carrier="SYN-CARRIER-1",
            as_of_age=timedelta(minutes=20),
            events=((2 * _D, "PICKED_UP", "SYN-HUB-WEST"), (1 * _D, "IN_TRANSIT", "SYN-HUB-CENTRAL")),
        ),
        "SYN-TRK-0004": _Scenario(
            carrier="SYN-CARRIER-2",
            as_of_age=timedelta(minutes=15),
            events=(
                (2 * _D, "PICKED_UP", "SYN-HUB-SOUTH"),
                (1 * _D, "IN_TRANSIT", "SYN-HUB-CENTRAL"),
                (2 * _H, "OUT_FOR_DELIVERY", "SYN-DEPOT-EAST"),
            ),
            estimated_delivery_in=timedelta(0),
        ),
        "SYN-TRK-0005": _Scenario(
            carrier="SYN-CARRIER-1",
            as_of_age=3 * _D,
            events=((5 * _D, "PICKED_UP", "SYN-HUB-NORTH"), (3 * _D, "IN_TRANSIT", "SYN-HUB-CENTRAL")),
        ),
        "SYN-TRK-0006": _Scenario(carrier="SYN-CARRIER-2", as_of_age=timedelta(0), unavailable=True),
    }
)


class SyntheticCarrierAdapter:
    """In-memory ``CarrierTrackingSource``. Holds no credentials; makes no calls."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock

    def get_tracking(self, carrier: str, tracking_number: str) -> CarrierTrackingResponse | None:
        scenario = _SCENARIOS.get(tracking_number)
        if scenario is None or scenario.carrier != carrier:
            return None
        if scenario.unavailable:
            raise CarrierUnavailableError("carrier source unavailable")
        now = self._clock()
        estimate = None
        if scenario.estimated_delivery_in is not None:
            estimate = (now + scenario.estimated_delivery_in).date().isoformat()
        return CarrierTrackingResponse(
            carrier=carrier,
            tracking_number=tracking_number,
            source=SOURCE_NAME,
            as_of=now - scenario.as_of_age,
            events=tuple(CarrierEvent(now - age, code, loc) for age, code, loc in scenario.events),
            exception_codes=scenario.exception_codes,
            estimated_delivery=estimate,
        )
