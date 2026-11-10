"""Structured audit events with an allowlisted, value-free payload (Step 2.6E).

[Proposal] Event names reuse the WF section 11 catalog where one fits
(``authorization.denied``, ``source.unavailable``); the ``tool.*``,
``model.*`` and ``tool_loop.*`` types are new and also [Proposal]. Common
fields follow WF section 11: event ID, type, timestamp, case ID, correlation
ID, actor and outcome [Decision D9; FR-AUD-01].

Safety by construction (FR-AUD-02): ``details`` accepts only allowlisted keys
with scalar values, so tool arguments, tool results, raw model responses,
exception messages and credentials cannot be attached. Callers pass codes and
class names, never free text from models or systems.

Not implemented: tamper evidence, retention and trail reconstruction
(FR-AUD-03, FR-AUD-04); the sinks here are in-memory or standard logging.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

SUCCESS = "success"
FAILURE = "failure"

# Detail keys that may appear on an event. Anything else is a programming error.
ALLOWED_DETAIL_KEYS = frozenset(
    {
        "tool_name",  # registered tool names only; None when the name was unsupported
        "tool_use_id",  # opaque model-generated ID, only if it matches _SAFE_ID
        "round",
        "error_code",
        "check",  # which authorization check failed: user, service or context
        "failure_class",  # exception class name or machine reason, never a message
        "source_system",
        "status_marked",
        "attempt",
        "max_attempts",
        "retryable",
        "will_retry",
        "backoff_seconds",
        "rounds",
        "tool_call_count",
        "model_id",
        "request_id",
    }
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SCALARS = (str, int, float, bool, type(None))


def safe_id(value: object) -> str | None:
    """Return ``value`` if it is a short opaque identifier, else ``"<invalid>"``."""
    if value is None:
        return None
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "<invalid>"


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    event_type: str
    timestamp: str  # ISO 8601, UTC
    correlation_id: str
    actor: str  # requesting user ID from the trusted AccessContext
    tenant_id: str
    outcome: str  # SUCCESS or FAILURE
    case_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in (SUCCESS, FAILURE):
            raise ValueError("outcome must be success or failure")
        unknown = set(self.details) - ALLOWED_DETAIL_KEYS
        if unknown:
            raise ValueError(f"audit detail keys not allowed: {sorted(unknown)}")
        if not all(isinstance(v, _SCALARS) for v in self.details.values()):
            raise ValueError("audit detail values must be scalars")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, object]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["details"] = dict(self.details)
        return data


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Collects events in order (tests and local runs)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]


class LoggingAuditSink:
    """Writes one JSON line per event to the ``northstar.audit`` logger at INFO."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("northstar.audit")

    def emit(self, event: AuditEvent) -> None:
        self._logger.info(json.dumps(event.to_dict(), sort_keys=True))


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Auditor:
    """Binds the common fields once and emits events to a sink.

    Sink errors propagate: if an event cannot be recorded the caller stops,
    rather than continuing without an audit trail (fail closed).
    """

    def __init__(
        self,
        sink: AuditSink,
        *,
        actor: str,
        tenant_id: str,
        correlation_id: str | None = None,
        case_id: str | None = None,
        clock: Callable[[], str] = utc_timestamp,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._sink = sink
        self._actor = actor
        self._tenant_id = tenant_id
        self.correlation_id = correlation_id or id_factory()
        self._case_id = case_id
        self._clock = clock
        self._id_factory = id_factory

    def emit(self, event_type: str, outcome: str, **details: object) -> AuditEvent:
        event = AuditEvent(
            event_id=self._id_factory(),
            event_type=event_type,
            timestamp=self._clock(),
            correlation_id=self.correlation_id,
            actor=self._actor,
            tenant_id=self._tenant_id,
            outcome=outcome,
            case_id=self._case_id,
            details=details,
        )
        self._sink.emit(event)
        return event
