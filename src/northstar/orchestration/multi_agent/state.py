"""Investigation state contract for a LangGraph candidate of alternative C.

[Proposal] LangGraph is a candidate only (ADR-002 Proposed); it is an
optional extra and is not imported here. The contract uses standard-library
``TypedDict`` plus ``Annotated`` reducers, which is the shape LangGraph's
``StateGraph`` accepts, so the same state can be tested offline. The Step 3.3
evidence nodes and the Step 3.4 conditional routing live in
``evidence_graph``; no routing, agent count or live calls are defined here, and the number of specialists stays a benchmark
parameter (AA sections 3.3, 8).

Reused Phase 2 types (not redefined): ``DeliveryStatus`` and
``DelayedDeliveryInvestigation`` from ``northstar.evidence.investigation_schema``;
the order ID format, freshness labels and verification reasons from the
read-only tools; ``safe_id`` from ``northstar.observability.audit``.

Fail closed [Decision D12, D15, D18; FR-EVD-01, FR-EVD-03 to FR-EVD-05]:

- The initial state has no evidence, ``delivery_status`` UNVERIFIED and
  ``requires_human_review`` true.
- Evidence is never invented: a record with no retrieval carries no payload,
  and provenance fields stay ``None`` unless a source supplied them.
- Freshness metadata must be self-consistent (Step 3.2): FRESH and STALE
  need an age, a limit and both timestamps; the age must match the
  timestamps; FRESH needs age <= limit and STALE age > limit, the same rule
  the carrier tool applies. UNKNOWN is allowed only when age or limit is
  missing. Missing values are never filled in.
- ``requires_human_review`` is always true; the platform is never an approver.

Trust boundary (Step 3.2). Two checks are kept separate:

1. Structural validation (``validate_evidence``): shape, field values,
   freshness consistency, no secrets, and that a VERIFIED claim names fresh,
   retrieved carrier evidence with provenance. It proves consistency only.
   Every ``EvidenceRecord`` field, including ``verification``, is a
   self-declared claim that anyone writing state can set.
2. Authoritative verification (``is_trusted_carrier_evidence``): the record
   was produced in this process by ``retrieve_carrier_evidence``, which runs
   the Phase 2 ``get_carrier_tracking`` tool with the trusted
   ``AccessContext`` from ``InvestigationRuntimeContext``. The tool
   authorizes the requesting user and the service scope before any read
   [Decision D16; FR-AUTH-01, FR-AUTH-02]; a denial raises and produces no
   evidence. The record is then sealed with an HMAC-SHA256 ``attestation``
   over all of its fields, keyed by a random per-process key that lives only
   in this module (never in state, checkpoints, configuration or model
   input). The attestation is not a trust flag: a caller can write the
   field, but cannot produce a value that verifies, and any later change to
   the record (status, freshness, payload, provenance) invalidates it.

   The HMAC is a temporary integrity safeguard for one process lifetime. It
   is not a durable trust mechanism: it proves only that this process
   retrieved the record, not that the record is still current or that the
   user now asking may see it. No persistent or managed signing key is used.

VERIFIED is accepted only when both checks pass. ``make_evidence`` cannot
create VERIFIED or attested records. ``effective_delivery_status`` reads
UNVERIFIED and ``validate_state`` raises for any VERIFIED claim (in
``delivery_status``, in either evidence slot, or in ``final_investigation``)
that is not backed by trusted carrier evidence.

Enforcement in graph nodes (Step 3.3 ``evidence_graph`` implements the
first two points for its OMS and carrier nodes, and its Step 3.4
``route_after_oms`` validates state before routing; later nodes must follow
them):

- Nodes must obtain carrier evidence only through
  ``retrieve_carrier_evidence`` and must turn ``AccessDeniedError``,
  ``OrderNotFoundError`` and input errors into ``SafeError`` items or
  non-retrieved evidence (``make_evidence``) rather than VERIFIED output.
- Nodes, routing and any final-answer step must read status through
  ``effective_delivery_status`` and run ``validate_state`` before using a
  state, never trust ``record["verification"]`` or ``delivery_status`` alone.
- The attestation binds the record to the order in its payload, not to the
  requesting user or tenant. Resume handling below covers this gap.

Checkpoint resume (Step 3.2 design choice selected by the user; not yet
recorded in BD section 12; enforcement is Step 3.6 work, not implemented):

- Checkpointed VERIFIED evidence is never trusted automatically after a
  resume, in any process. Its attestation may still verify in the same
  process, but the evidence may be stale and the resuming user may differ.
- Before accepting VERIFIED after a resume, Step 3.6 must reauthorize the
  current user from a newly built ``InvestigationRuntimeContext`` and
  re-fetch carrier evidence through ``retrieve_carrier_evidence``, replacing
  the checkpointed ``carrier_evidence``, ``delivery_status`` and any VERIFIED
  ``final_investigation``.
- Fail closed: failed reauthorization (``AccessDeniedError``), an unknown or
  other-tenant order, an unavailable carrier, or invalid or untrusted
  evidence leaves the case UNVERIFIED with ``requires_human_review`` true,
  plus a ``SafeError``. Checkpointed evidence is never used as a fallback
  [Decision D15].
- Current behavior that Step 3.6 relies on: in a new process the key
  differs, so ``effective_delivery_status`` reads UNVERIFIED and
  ``validate_state`` rejects the stale VERIFIED claim. Step 3.6 therefore
  needs a downgrade step that discards untrusted evidence before
  ``validate_state`` runs, so a resumed case can recover.
- The HMAC key and the ``InvestigationRuntimeContext`` (with its
  ``AccessContext``) never enter state, checkpoints, ``config`` or model
  input. Only the attestation digest is checkpointed; it is not a secret and
  cannot be used to forge another record. Model input gets tool results
  (``payload``) only, never whole evidence records.

Parallel updates. In LangGraph a key without a reducer accepts one write per
step and raises on concurrent writes, which is the desired fail-closed
behavior for single-writer fields (``order_id``, ``oms_evidence``,
``carrier_evidence``, ``delivery_status``, ``final_investigation``). OMS and
carrier retrieval write different keys, so they can run in parallel without
a reducer. Reducers are used only where concurrent writes are expected and
safe to merge:

- ``errors``: ``merge_errors`` appends validated ``SafeError`` items.
- ``requires_human_review``: ``keep_human_review`` always returns true, so
  any number of writers cannot clear it.

``delivery_status`` deliberately has no reducer: a merge rule cannot tell
concurrent writes from a later verification step, so it stays single-writer.

Trusted runtime context (not checkpointed). Checkpointed state holds case
data only: no ``AccessContext``, credentials, tokens or adapters.
``validate_state`` rejects unknown keys and any payload that holds
non-JSON values (for example an ``AccessContext``) or credential-like keys.
The ``AccessContext`` is built per invocation by trusted application code
from the authenticated session [Decision D16; FR-AUTH-01, FR-AUTH-02] and
supplied as ``InvestigationRuntimeContext``, for example through LangGraph's
runtime context (``graph.invoke(state, context=...)`` with
``context_schema=InvestigationRuntimeContext``). Nodes read it from the
runtime and pass it to tools as ``access_context=``. [Assumption] The
runtime context is not persisted by the checkpointer; this must be verified
against the pinned LangGraph version during the ADR-002 section 5
validation. It is never placed in state, in ``config["configurable"]``
(checkpoint metadata may record configuration) or in model input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Mapping, TypedDict

from northstar.evidence.investigation_schema import (
    DelayedDeliveryInvestigation,
    DeliveryStatus,
)
from northstar.integrations.synthetic_carrier import CarrierTrackingSource, Clock, utc_now
from northstar.observability.audit import safe_id
from northstar.security.access_context import AccessContext
from northstar.tools.carrier_tracking import (
    FRESH,
    FRESHNESS_UNKNOWN,
    STALE,
    SYNTHETIC_FRESHNESS_LIMIT,
    get_carrier_tracking,
)
from northstar.tools.order_details import ORDER_ID_PATTERN

# Source systems for this slice. Current status comes only from these APIs;
# Snowflake is historical context and is not a source of current status
# [Decision D15].
SOURCE_OMS = "OMS"
SOURCE_CARRIER = "CARRIER"
SourceSystem = Literal["OMS", "CARRIER"]
_SOURCE_SYSTEMS = frozenset({SOURCE_OMS, SOURCE_CARRIER})

# Outcome of the retrieval call itself, separate from verification.
# [Proposal] Names are illustrative.
RETRIEVED = "RETRIEVED"
NOT_FOUND = "NOT_FOUND"
UNAVAILABLE = "UNAVAILABLE"
DENIED = "DENIED"
ERROR = "ERROR"
RetrievalStatus = Literal["RETRIEVED", "NOT_FOUND", "UNAVAILABLE", "DENIED", "ERROR"]
_RETRIEVAL_STATUSES = frozenset({RETRIEVED, NOT_FOUND, UNAVAILABLE, DENIED, ERROR})

FreshnessStatus = Literal["FRESH", "STALE", "UNKNOWN"]
_FRESHNESS = frozenset({FRESH, STALE, FRESHNESS_UNKNOWN})

_ORDER_ID_RE = re.compile(ORDER_ID_PATTERN)

# Payload keys that suggest secrets or identity material. Defense in depth
# only; the primary control is that trusted context never enters state.
_FORBIDDEN_KEY_PARTS = (
    "access_context",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "session",
    "card_number",
    "cvv",
)
_JSON_SCALARS = (str, int, float, bool, type(None))

# Per-process attestation key (Step 3.2). Held only here; never written to
# state, checkpoints, logs or model input. A managed key is [Open].
_ATTESTATION_KEY = secrets.token_bytes(32)
_ATTESTATION_RE = re.compile(r"[0-9a-f]{64}")
# Tolerance when re-deriving age from ISO timestamps (int truncation).
_AGE_TOLERANCE_SECONDS = 1


class InvalidStateError(ValueError):
    """The state does not meet the contract. Messages name fields, never values."""


class SafeError(TypedDict):
    """A value-free error record: codes only, no messages or exception text."""

    node: str
    error_code: str
    source_system: SourceSystem | None
    retryable: bool


class Freshness(TypedDict):
    status: FreshnessStatus
    age_seconds: int | None
    limit_seconds: int | None


class EvidenceRecord(TypedDict):
    """One retrieval from a current-status source, with provenance (FR-EVD-01).

    ``payload`` is the tool result as returned (JSON-safe data, never
    instructions [FR-EVD-07]); it is ``None`` when nothing was retrieved.
    All fields are self-declared claims; ``attestation`` is set only by
    ``retrieve_carrier_evidence`` and checked by
    ``is_trusted_carrier_evidence`` (see the module trust boundary).
    """

    source_system: SourceSystem
    retrieval_status: RetrievalStatus
    verification: DeliveryStatus
    reason: str | None
    source: str | None
    retrieved_at: str | None
    source_as_of: str | None
    freshness: Freshness
    payload: dict[str, Any] | None
    attestation: str | None


def _require_safe_code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or safe_id(value) != value:
        raise InvalidStateError(f"{field_name} must be a short opaque code")
    return value


def make_safe_error(
    node: str,
    error_code: str,
    *,
    source_system: str | None = None,
    retryable: bool = False,
) -> SafeError:
    """Build a ``SafeError``; free text is rejected by the code pattern."""
    _require_safe_code(node, "node")
    _require_safe_code(error_code, "error_code")
    if source_system is not None and source_system not in _SOURCE_SYSTEMS:
        raise InvalidStateError("source_system is not supported")
    if type(retryable) is not bool:
        raise InvalidStateError("retryable must be a boolean")
    return SafeError(
        node=node,
        error_code=error_code,
        source_system=source_system,  # type: ignore[typeddict-item]
        retryable=retryable,
    )


def _validate_safe_error(item: object) -> SafeError:
    if not isinstance(item, dict) or set(item) != set(SafeError.__annotations__):
        raise InvalidStateError("errors items must be SafeError records")
    return make_safe_error(
        item["node"],
        item["error_code"],
        source_system=item["source_system"],
        retryable=item["retryable"],
    )


def merge_errors(current: list[SafeError] | None, update: list[SafeError] | None) -> list[SafeError]:
    """Reducer: append validated errors; never drops or rewrites earlier ones."""
    if update is None:
        update = []
    if not isinstance(update, list) or not isinstance(current, (list, type(None))):
        raise InvalidStateError("errors must be a list")
    return [*(current or []), *(_validate_safe_error(item) for item in update)]


def keep_human_review(current: object, update: object) -> Literal[True]:
    """Reducer: human review stays required whatever any writer sends [Decision D18]."""
    return True


class InvestigationState(TypedDict):
    """Checkpointable state for one delayed-delivery investigation."""

    order_id: str
    oms_evidence: EvidenceRecord | None
    carrier_evidence: EvidenceRecord | None
    delivery_status: DeliveryStatus
    final_investigation: DelayedDeliveryInvestigation | None
    requires_human_review: Annotated[Literal[True], keep_human_review]
    errors: Annotated[list[SafeError], merge_errors]


@dataclass(frozen=True)
class InvestigationRuntimeContext:
    """Trusted per-invocation context; never stored in state or checkpoints."""

    access_context: AccessContext

    def __post_init__(self) -> None:
        if not isinstance(self.access_context, AccessContext):
            raise TypeError("access_context must be an AccessContext")


def _require_order_id(value: object) -> str:
    if not isinstance(value, str) or not _ORDER_ID_RE.fullmatch(value):
        raise InvalidStateError("order_id has an invalid format")
    return value


def initial_state(order_id: str) -> InvestigationState:
    """Return the fail-closed starting state: no evidence, UNVERIFIED, review required."""
    return InvestigationState(
        order_id=_require_order_id(order_id),
        oms_evidence=None,
        carrier_evidence=None,
        delivery_status=DeliveryStatus.UNVERIFIED,
        final_investigation=None,
        requires_human_review=True,
        errors=[],
    )


def _is_aware_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _check_payload(value: object, depth: int = 0) -> None:
    """Payload must be plain JSON data without credential-like keys."""
    if depth > 8:
        raise InvalidStateError("payload is nested too deeply")
    if isinstance(value, _JSON_SCALARS) and not isinstance(value, DeliveryStatus):
        return
    if isinstance(value, list):
        for item in value:
            _check_payload(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidStateError("payload keys must be strings")
            lowered = key.lower()
            if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                raise InvalidStateError("payload contains a forbidden key")
            _check_payload(item, depth + 1)
        return
    raise InvalidStateError("payload must contain JSON data only")


def _supports_verified(record: EvidenceRecord) -> bool:
    freshness = record["freshness"]
    return (
        record["source_system"] == SOURCE_CARRIER
        and record["retrieval_status"] == RETRIEVED
        and record["reason"] is None
        and record["payload"] is not None
        and isinstance(record["source"], str)
        and record["source"].strip() != ""
        and _is_aware_iso(record["retrieved_at"])
        and _is_aware_iso(record["source_as_of"])
        and freshness["status"] == FRESH
    )


def _check_freshness(record: Mapping[str, Any]) -> None:
    """Reject contradictory freshness metadata; never fill in missing values.

    Mirrors the carrier tool: age = retrieved_at - source_as_of, FRESH when
    age <= limit, STALE when age > limit, UNKNOWN when either is unknown.
    """
    freshness = record["freshness"]
    if not isinstance(freshness, dict) or set(freshness) != set(Freshness.__annotations__):
        raise InvalidStateError("freshness must have exactly the Freshness fields")
    status = freshness["status"]
    if status not in _FRESHNESS:
        raise InvalidStateError("freshness status is not supported")
    age, limit = freshness["age_seconds"], freshness["limit_seconds"]
    for name, value in (("age_seconds", age), ("limit_seconds", limit)):
        if value is not None and (type(value) is not int or value < 0):
            raise InvalidStateError(f"freshness {name} must be a non-negative int or None")

    if age is not None:
        # An age is only meaningful if it can be checked against both times.
        if record["retrieved_at"] is None or record["source_as_of"] is None:
            raise InvalidStateError("freshness age_seconds requires retrieved_at and source_as_of")
        derived = (
            datetime.fromisoformat(record["retrieved_at"])
            - datetime.fromisoformat(record["source_as_of"])
        ).total_seconds()
        if abs(derived - age) > _AGE_TOLERANCE_SECONDS:
            raise InvalidStateError("freshness age_seconds does not match the timestamps")

    if status == FRESHNESS_UNKNOWN:
        if age is not None and limit is not None:
            raise InvalidStateError("freshness UNKNOWN contradicts a known age and limit")
        return
    if age is None or limit is None:
        raise InvalidStateError(f"freshness {status} requires age_seconds and limit_seconds")
    if status == FRESH and age > limit:
        raise InvalidStateError("freshness FRESH contradicts age_seconds above limit_seconds")
    if status == STALE and age <= limit:
        raise InvalidStateError("freshness STALE contradicts age_seconds within limit_seconds")


def validate_evidence(record: object) -> EvidenceRecord:
    """Structural check only: raise ``InvalidStateError`` unless ``record`` is consistent.

    Passing this check does not prove the record is authentic; see
    ``is_trusted_carrier_evidence`` for that.
    """
    if not isinstance(record, dict) or set(record) != set(EvidenceRecord.__annotations__):
        raise InvalidStateError("evidence must have exactly the EvidenceRecord fields")
    if record["source_system"] not in _SOURCE_SYSTEMS:
        raise InvalidStateError("source_system is not supported")
    if record["retrieval_status"] not in _RETRIEVAL_STATUSES:
        raise InvalidStateError("retrieval_status is not supported")
    if not isinstance(record["verification"], DeliveryStatus):
        raise InvalidStateError("verification must be a DeliveryStatus")
    if record["reason"] is not None:
        _require_safe_code(record["reason"], "reason")
    if record["source"] is not None and not (
        isinstance(record["source"], str) and record["source"].strip()
    ):
        raise InvalidStateError("source must be a non-blank string or None")
    for name in ("retrieved_at", "source_as_of"):
        if record[name] is not None and not _is_aware_iso(record[name]):
            raise InvalidStateError(f"{name} must be a timezone-aware ISO 8601 string or None")
    attestation = record["attestation"]
    if attestation is not None and not (
        isinstance(attestation, str) and _ATTESTATION_RE.fullmatch(attestation)
    ):
        raise InvalidStateError("attestation must be a hex digest or None")

    _check_freshness(record)
    freshness = record["freshness"]

    payload = record["payload"]
    if payload is not None:
        if not isinstance(payload, dict):
            raise InvalidStateError("payload must be an object or None")
        _check_payload(payload)
    if record["retrieval_status"] != RETRIEVED:
        # Nothing was retrieved, so nothing may be presented as retrieved.
        if payload is not None or record["source_as_of"] is not None:
            raise InvalidStateError("evidence without a retrieval must not carry data")
        if freshness["status"] != FRESHNESS_UNKNOWN:
            raise InvalidStateError("evidence without a retrieval has unknown freshness")

    if record["verification"] is DeliveryStatus.VERIFIED:
        if not _supports_verified(record):
            raise InvalidStateError(
                "VERIFIED requires fresh, retrieved carrier evidence with provenance"
            )
        if attestation is None:
            raise InvalidStateError("VERIFIED requires an attestation from the trusted carrier path")
    return record  # type: ignore[return-value]


def _attestation_digest(record: Mapping[str, Any]) -> str:
    """HMAC over every field except ``attestation``, in a canonical encoding."""
    body = {key: record[key] for key in EvidenceRecord.__annotations__ if key != "attestation"}
    body["verification"] = DeliveryStatus(body["verification"]).value
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(_ATTESTATION_KEY, encoded.encode("ascii"), hashlib.sha256).hexdigest()


def is_trusted_carrier_evidence(record: object) -> bool:
    """Authoritative check: True only for unmodified output of ``retrieve_carrier_evidence``.

    Never raises; anything unexpected (bad shape, forged or missing
    attestation, a key from another process) is untrusted.
    """
    try:
        validated = validate_evidence(record)
        attestation = validated["attestation"]
        return (
            validated["source_system"] == SOURCE_CARRIER
            and attestation is not None
            and hmac.compare_digest(attestation, _attestation_digest(validated))
        )
    except (InvalidStateError, AttributeError, KeyError, TypeError, ValueError):
        return False


def _build_evidence(
    source_system: str,
    retrieval_status: str,
    *,
    verification: DeliveryStatus,
    reason: str | None,
    source: str | None,
    retrieved_at: str | None,
    source_as_of: str | None,
    freshness_status: str,
    age_seconds: int | None,
    limit_seconds: int | None,
    payload: Mapping[str, Any] | None,
) -> EvidenceRecord:
    return EvidenceRecord(
        source_system=source_system,  # type: ignore[typeddict-item]
        retrieval_status=retrieval_status,  # type: ignore[typeddict-item]
        verification=verification,
        reason=reason,
        source=source,
        retrieved_at=retrieved_at,
        source_as_of=source_as_of,
        freshness=Freshness(
            status=freshness_status,  # type: ignore[typeddict-item]
            age_seconds=age_seconds,
            limit_seconds=limit_seconds,
        ),
        payload=dict(payload) if isinstance(payload, Mapping) else payload,  # type: ignore[arg-type]
        attestation=None,
    )


def make_evidence(
    source_system: str,
    retrieval_status: str,
    *,
    reason: str | None = None,
    source: str | None = None,
    retrieved_at: str | None = None,
    source_as_of: str | None = None,
    freshness_status: str = FRESHNESS_UNKNOWN,
    age_seconds: int | None = None,
    limit_seconds: int | None = None,
    payload: Mapping[str, Any] | None = None,
) -> EvidenceRecord:
    """Build and validate an UNVERIFIED, unattested ``EvidenceRecord``.

    For OMS evidence and for retrievals that did not happen (DENIED,
    UNAVAILABLE and so on). It cannot produce VERIFIED: only
    ``retrieve_carrier_evidence`` can.
    """
    return validate_evidence(
        _build_evidence(
            source_system,
            retrieval_status,
            verification=DeliveryStatus.UNVERIFIED,
            reason=reason,
            source=source,
            retrieved_at=retrieved_at,
            source_as_of=source_as_of,
            freshness_status=freshness_status,
            age_seconds=age_seconds,
            limit_seconds=limit_seconds,
            payload=payload,
        )
    )


def _evidence_from_carrier_result(result: Mapping[str, Any]) -> EvidenceRecord:
    """Map a ``get_carrier_tracking`` result to an unattested record, copying provenance only.

    VERIFIED is carried over only if the tool reported it; any shape or
    consistency problem raises rather than guessing. Private: the result is
    trusted only when it comes straight from the tool call in
    ``retrieve_carrier_evidence``.
    """
    try:
        status = result["current_delivery_status"]
        freshness = result["freshness"]
        record = _build_evidence(
            SOURCE_CARRIER,
            RETRIEVED,
            verification=DeliveryStatus(status["verification"]),
            reason=status["reason"],
            source=result["source"],
            retrieved_at=result["retrieved_at"],
            source_as_of=result["source_as_of"],
            freshness_status=freshness["status"],
            age_seconds=freshness["age_seconds"],
            limit_seconds=freshness["limit_seconds"],
            payload=result,
        )
    except (KeyError, TypeError, ValueError):
        raise InvalidStateError("carrier result has an unexpected shape") from None
    # The attestation slot is still empty, so check everything else: the
    # structure (as an UNVERIFIED view), then the VERIFIED preconditions.
    validate_evidence({**record, "verification": DeliveryStatus.UNVERIFIED})
    if record["verification"] is DeliveryStatus.VERIFIED and not _supports_verified(record):
        raise InvalidStateError("VERIFIED requires fresh, retrieved carrier evidence with provenance")
    return record


def retrieve_carrier_evidence(
    order_id: object,
    *,
    runtime_context: InvestigationRuntimeContext,
    adapter: CarrierTrackingSource | None = None,
    clock: Clock = utc_now,
    freshness_limit: timedelta = SYNTHETIC_FRESHNESS_LIMIT,
) -> EvidenceRecord:
    """The only producer of trusted carrier evidence (and so of VERIFIED).

    Runs the Phase 2 ``get_carrier_tracking`` tool with the trusted
    ``AccessContext``; the tool authorizes the read before retrieving.
    ``AccessDeniedError``, ``OrderNotFoundError`` and input errors propagate
    unchanged, so a failed or denied call yields no evidence. ``adapter``,
    ``clock`` and ``freshness_limit`` are supplied by trusted code only.
    """
    if not isinstance(runtime_context, InvestigationRuntimeContext):
        raise TypeError("runtime_context must be an InvestigationRuntimeContext")
    result = get_carrier_tracking(
        order_id,
        access_context=runtime_context.access_context,
        adapter=adapter,
        clock=clock,
        freshness_limit=freshness_limit,
    )
    record = _evidence_from_carrier_result(result)
    record["attestation"] = _attestation_digest(record)
    return validate_evidence(record)


def effective_delivery_status(state: Mapping[str, Any]) -> DeliveryStatus:
    """Fail-closed read: VERIFIED only if the state claims it and trusted carrier evidence agrees."""
    try:
        if state.get("delivery_status") is not DeliveryStatus.VERIFIED:
            return DeliveryStatus.UNVERIFIED
        carrier = state.get("carrier_evidence")
        if (
            carrier is not None
            and is_trusted_carrier_evidence(carrier)
            and carrier["verification"] is DeliveryStatus.VERIFIED
        ):
            return DeliveryStatus.VERIFIED
    except (AttributeError, KeyError, TypeError):
        pass
    return DeliveryStatus.UNVERIFIED


def validate_state(state: object) -> InvestigationState:
    """Raise ``InvalidStateError`` unless ``state`` meets the contract."""
    if not isinstance(state, dict):
        raise InvalidStateError("state must be a mapping")
    expected = set(InvestigationState.__annotations__)
    if set(state) - expected:
        # Blocks AccessContext, credentials, tokens and any other extra key.
        raise InvalidStateError("state contains unexpected keys")
    if expected - set(state):
        raise InvalidStateError("state is missing required keys")

    _require_order_id(state["order_id"])
    for name, system in (("oms_evidence", SOURCE_OMS), ("carrier_evidence", SOURCE_CARRIER)):
        if state[name] is not None:
            record = validate_evidence(state[name])
            if record["source_system"] != system:
                raise InvalidStateError(f"{name} has the wrong source_system")
            payload_order = (record["payload"] or {}).get("order_id")
            if payload_order is not None and payload_order != state["order_id"]:
                raise InvalidStateError(f"{name} is for a different order")
            if record["verification"] is DeliveryStatus.VERIFIED and not (
                is_trusted_carrier_evidence(record)
            ):
                # A structurally valid VERIFIED claim is still self-declared.
                raise InvalidStateError(f"{name} VERIFIED is not from the trusted carrier path")

    if not isinstance(state["delivery_status"], DeliveryStatus):
        raise InvalidStateError("delivery_status must be a DeliveryStatus")
    if (
        state["delivery_status"] is DeliveryStatus.VERIFIED
        and effective_delivery_status(state) is not DeliveryStatus.VERIFIED
    ):
        raise InvalidStateError("delivery_status VERIFIED is not supported by trusted carrier evidence")

    if state["requires_human_review"] is not True:
        raise InvalidStateError("requires_human_review must be true")

    final = state["final_investigation"]
    if final is not None:
        if not isinstance(final, DelayedDeliveryInvestigation):
            raise InvalidStateError("final_investigation must be a DelayedDeliveryInvestigation")
        if final.order_id != state["order_id"]:
            raise InvalidStateError("final_investigation is for a different order")
        if (
            final.delivery_status is DeliveryStatus.VERIFIED
            and effective_delivery_status(state) is not DeliveryStatus.VERIFIED
        ):
            raise InvalidStateError("final_investigation claims VERIFIED without verified evidence")

    if not isinstance(state["errors"], list):
        raise InvalidStateError("errors must be a list")
    for item in state["errors"]:
        _validate_safe_error(item)
    return state  # type: ignore[return-value]
