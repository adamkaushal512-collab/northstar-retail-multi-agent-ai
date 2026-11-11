"""OMS-first evidence graph with parallel fan-out and fan-in (Steps 3.3 to 3.5).

[Proposal] LangGraph is a candidate only (ADR-002 Proposed). This module is
part of the LangGraph hands-on implementation used to benchmark the B and C
candidates (ADR-002 section 3, AA section 8). It does not select an
architecture or an agent count: it has no model, coordinator or specialist
nodes, only deterministic nodes over the Phase 2 read-only tools, one
deterministic routing function and one validation barrier. Requires the
optional ``langgraph`` extra.

Graph (Step 3.5)::

    START -> retrieve_oms_evidence --route_after_oms--+--> retrieve_carrier_evidence --+
                                   |                  +--> analyze_oms_evidence -------+
                                   |                                                   v
                                   +--> END (OMS not RETRIEVED)       validate_merged_evidence -> END

``route_after_oms`` is a pure function of the validated state. Only when
``oms_evidence`` is valid OMS evidence with ``retrieval_status`` RETRIEVED
and a usable payload for the requested order does it return
``ROUTE_TO_EVIDENCE``, which fans out to the carrier node and the OMS
analysis node in the same LangGraph superstep (they run concurrently).
DENIED, NOT_FOUND, UNAVAILABLE, ERROR and every other unsuccessful OMS
retrieval end the run with no carrier call and no analysis,
``delivery_status`` UNVERIFIED, ``requires_human_review`` true and the OMS
node's ``SafeError``. Missing, malformed or contradictory OMS evidence raises
``InvalidStateError``. It never reads route flags, error strings, model
output or runtime context.

Fan-in (Step 3.5). ``validate_merged_evidence`` is attached with a LangGraph
waiting edge (``add_edge([carrier, analysis], join)``), so it runs once,
only after both branches have finished and their updates have been merged.
It writes nothing; it validates the merged state and raises
``InvalidStateError`` if the merge broke the contract (for example missing
carrier evidence or OMS evidence that is no longer usable).

Writer ownership. Parallel branches write disjoint keys: the carrier node
writes ``carrier_evidence`` and ``delivery_status``; the analysis node writes
``oms_analysis``; both may append to ``errors`` (``merge_errors`` reducer,
order between the two branches not guaranteed). These keys have no reducer,
so a second writer in the same step is rejected by LangGraph (fail closed).

No checkpointer, retries, recovery, model nodes or human approval steps
(later steps; human steps stay outside LangGraph, ADR-002 section 3.4).

Trust boundary [Decision D16; FR-AUTH-01, FR-AUTH-02]:

- Identity and permissions come only from the trusted
  ``InvestigationRuntimeContext`` passed through LangGraph's runtime context
  (``graph.invoke(state, context=...)``, ``context_schema``). Nodes read
  ``runtime.context`` and never read identity, permissions, credentials or
  attestations from state. A missing or wrong-typed runtime context is
  passed to the tools as ``None``, which they deny (fail closed).
- Authorization is enforced by the existing tools at each retrieval:
  ``get_order_details`` for OMS and ``retrieve_carrier_evidence`` (which runs
  ``get_carrier_tracking``) for carrier. Nodes add no authorization logic.
- ``adapter``, ``clock`` and ``freshness_limit`` are fixed by trusted code
  when the graph is built, never taken from state or model input.

Fail closed [Decision D12, D15, D18; FR-EVD-01, FR-EVD-03 to FR-EVD-05]:

- Each node runs ``validate_state`` on its input and on its proposed result
  and raises ``InvalidStateError`` on a contract violation, so an invalid
  state (for example a forged VERIFIED claim) stops the run.
- Denied, not-found, unavailable, stale, malformed or otherwise failed
  retrievals produce UNVERIFIED evidence without invented data plus a
  value-free ``SafeError`` naming the node, a code and the source system.
  Exception text is never stored.
- ``delivery_status`` is written only by the carrier node and only through
  ``effective_delivery_status``, so it is VERIFIED only for fresh, attested
  carrier evidence. OMS evidence never verifies delivery.
- ``requires_human_review`` stays true (reducer); no node writes it.
- Routing adds no authorization: a carrier retrieval reached through the
  router is still authorized by its own tool call, and an OMS success
  never verifies delivery.
- The OMS analysis node retrieves nothing and calls no tool: it reads only
  OMS evidence that this run's authorized OMS retrieval produced, derives
  ``oms_analysis`` deterministically (``derive_oms_analysis``) and never
  changes evidence or ``delivery_status``. Unusable input gives
  ``oms_analysis`` ``None`` plus a ``SafeError``; nothing is invented.

[Open] ``SafeError.retryable`` is always false here; retry classification
belongs to recovery (later step) and NFR retry targets (Step 1.4).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal, Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from northstar.evidence.investigation_schema import DeliveryStatus
from northstar.integrations.synthetic_carrier import CarrierTrackingSource, Clock, utc_now
from northstar.orchestration.multi_agent.state import (
    DENIED,
    ERROR,
    NOT_FOUND,
    RETRIEVED,
    SOURCE_CARRIER,
    SOURCE_OMS,
    EvidenceRecord,
    InvalidStateError,
    InvestigationRuntimeContext,
    InvestigationState,
    SafeError,
    derive_oms_analysis,
    effective_delivery_status,
    make_evidence,
    make_safe_error,
    retrieve_carrier_evidence,
    validate_state,
)
from northstar.security.access_context import AccessContext, AccessDeniedError
from northstar.tools.carrier_tracking import SYNTHETIC_FRESHNESS_LIMIT
from northstar.tools.order_details import (
    InvalidToolInputError,
    OrderNotFoundError,
    get_order_details,
)

OMS_NODE = "retrieve_oms_evidence"
CARRIER_NODE = "retrieve_carrier_evidence"
ANALYSIS_NODE = "analyze_oms_evidence"
JOIN_NODE = "validate_merged_evidence"
# Branches that run in parallel after a successful OMS retrieval. Their
# relative order is not defined.
PARALLEL_NODES = (CARRIER_NODE, ANALYSIS_NODE)

# Exactly the fields ``get_order_details`` returns (``dict[str, str]``); a
# RETRIEVED OMS payload must have these keys and no others, all strings.
# A test checks this set against the tool's real output.
OMS_PAYLOAD_FIELDS = frozenset({"order_id", "order_status", "promised_delivery_date", "carrier"})
# Routing targets after the OMS node; also the conditional-edge path map.
# ROUTE_TO_EVIDENCE is a tuple, so LangGraph schedules both branches.
ROUTE_TO_EVIDENCE = PARALLEL_NODES
ROUTE_TO_END = END
OMS_ROUTES = {CARRIER_NODE: CARRIER_NODE, ANALYSIS_NODE: ANALYSIS_NODE, ROUTE_TO_END: END}

# Value-free error codes. [Proposal] Names are illustrative.
ACCESS_DENIED = "ACCESS_DENIED"
ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
INVALID_ORDER_ID = "INVALID_ORDER_ID"
INVALID_OMS_EVIDENCE = "INVALID_OMS_EVIDENCE"
INVALID_CARRIER_EVIDENCE = "INVALID_CARRIER_EVIDENCE"
OMS_RETRIEVAL_ERROR = "OMS_RETRIEVAL_ERROR"
CARRIER_RETRIEVAL_ERROR = "CARRIER_RETRIEVAL_ERROR"
INVALID_OMS_ANALYSIS = "INVALID_OMS_ANALYSIS"
OMS_ANALYSIS_ERROR = "OMS_ANALYSIS_ERROR"


def _runtime_context(
    runtime: Runtime[InvestigationRuntimeContext],
) -> InvestigationRuntimeContext | None:
    """Trusted context from the LangGraph runtime only; anything else is None."""
    context = getattr(runtime, "context", None)
    return context if isinstance(context, InvestigationRuntimeContext) else None


def _access_context(runtime: Runtime[InvestigationRuntimeContext]) -> AccessContext | None:
    """``None`` is denied by the tools' ``require_read`` (check "context")."""
    context = _runtime_context(runtime)
    return context.access_context if context is not None else None


def _failure(
    node: str, source_system: str, retrieval_status: str, code: str
) -> tuple[EvidenceRecord, SafeError]:
    """Non-retrieved evidence (no payload, unknown freshness) plus its error record."""
    evidence = make_evidence(source_system, retrieval_status, reason=code)
    return evidence, make_safe_error(node, code, source_system=source_system)


def _checked(state: Mapping[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Validate the state the update would produce; raise rather than emit it."""
    errors = [*state["errors"], *update.get("errors", [])]
    validate_state({**state, **update, "errors": errors})
    return update


def _usable_oms_evidence(state: Mapping[str, Any]) -> EvidenceRecord | None:
    """Validated RETRIEVED OMS evidence, ``None`` for any other status, or raise.

    Shared by the router, the analysis node and the fan-in barrier so all
    three apply one definition of a successful OMS retrieval.
    """
    validated = validate_state(state)
    oms = validated["oms_evidence"]
    if oms is None:
        raise InvalidStateError("oms_evidence is missing after OMS retrieval")
    # validate_state has already checked shape, source_system and order match.
    if oms["retrieval_status"] != RETRIEVED:
        return None
    if oms["reason"] is not None or oms["attestation"] is not None:
        raise InvalidStateError("oms_evidence RETRIEVED carries a failure reason or attestation")
    if oms["verification"] is not DeliveryStatus.UNVERIFIED:
        raise InvalidStateError("oms_evidence must be UNVERIFIED")
    payload = oms["payload"]
    if not isinstance(payload, dict) or set(payload) != OMS_PAYLOAD_FIELDS:
        raise InvalidStateError("oms_evidence RETRIEVED has no usable payload")
    if any(not isinstance(payload[name], str) for name in OMS_PAYLOAD_FIELDS):
        raise InvalidStateError("oms_evidence payload fields must be strings")
    if payload["order_id"] != validated["order_id"]:
        raise InvalidStateError("oms_evidence is for a different order")
    return oms


def route_after_oms(
    state: Mapping[str, Any],
) -> tuple[str, str] | Literal["__end__"]:
    """Deterministic, pure routing decision after the OMS node.

    Validates the complete state first (``validate_state``) and decides from
    ``oms_evidence`` alone. Returns ``ROUTE_TO_EVIDENCE`` (fan out to the
    carrier and OMS analysis nodes) only for RETRIEVED OMS evidence whose
    payload matches the ``get_order_details`` contract (exactly
    ``OMS_PAYLOAD_FIELDS``, all strings) for ``state["order_id"]``; returns
    ``ROUTE_TO_END`` for any other retrieval status (fail closed, no carrier
    call, no analysis). Raises ``InvalidStateError`` when the state is invalid
    or the OMS evidence is missing or contradicts itself (for example
    RETRIEVED with a reason code, an attestation or an unusable payload), so
    forged evidence stops the run instead of being treated as a success.

    Never reads route flags, ``errors``, ``delivery_status``, model output or
    runtime context, and has no side effects.
    """
    return ROUTE_TO_EVIDENCE if _usable_oms_evidence(state) is not None else ROUTE_TO_END


def validate_merged_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    """Fan-in barrier check after both parallel branches; writes nothing.

    Raises ``InvalidStateError`` unless the merged state is valid, the OMS
    evidence is still a usable RETRIEVED record and the carrier branch wrote
    its evidence. ``oms_analysis`` may be ``None`` (analysis failed closed and
    recorded a ``SafeError``); if present, ``validate_state`` has re-derived it.
    """
    if _usable_oms_evidence(state) is None:
        raise InvalidStateError("merged state lacks RETRIEVED OMS evidence")
    if state["carrier_evidence"] is None:
        raise InvalidStateError("merged state lacks carrier evidence")
    return {}


def build_evidence_graph(
    *,
    carrier_adapter: CarrierTrackingSource | None = None,
    clock: Clock = utc_now,
    freshness_limit: timedelta = SYNTHETIC_FRESHNESS_LIMIT,
):
    """Compile the evidence graph (OMS; on success, carrier and analysis in parallel, then fan-in).

    The keyword arguments are trusted wiring (defaults: synthetic carrier
    adapter, UTC clock, synthetic freshness limit). Invoke with
    ``graph.invoke(initial_state(order_id), context=InvestigationRuntimeContext(...))``.
    """

    def oms_node(
        state: InvestigationState, runtime: Runtime[InvestigationRuntimeContext]
    ) -> dict[str, Any]:
        validate_state(state)
        try:
            payload = get_order_details(state["order_id"], access_context=_access_context(runtime))
            evidence = make_evidence(
                SOURCE_OMS, RETRIEVED, retrieved_at=clock().isoformat(), payload=payload
            )
        except AccessDeniedError:
            evidence, error = _failure(OMS_NODE, SOURCE_OMS, DENIED, ACCESS_DENIED)
        except OrderNotFoundError:
            evidence, error = _failure(OMS_NODE, SOURCE_OMS, NOT_FOUND, ORDER_NOT_FOUND)
        except InvalidToolInputError:
            evidence, error = _failure(OMS_NODE, SOURCE_OMS, ERROR, INVALID_ORDER_ID)
        except InvalidStateError:
            evidence, error = _failure(OMS_NODE, SOURCE_OMS, ERROR, INVALID_OMS_EVIDENCE)
        except Exception:  # noqa: BLE001 - any fault means no evidence [Decision D15]
            evidence, error = _failure(OMS_NODE, SOURCE_OMS, ERROR, OMS_RETRIEVAL_ERROR)
        else:
            return _checked(state, {"oms_evidence": evidence})
        return _checked(state, {"oms_evidence": evidence, "errors": [error]})

    def carrier_node(
        state: InvestigationState, runtime: Runtime[InvestigationRuntimeContext]
    ) -> dict[str, Any]:
        validate_state(state)
        context = _runtime_context(runtime)
        errors: list[SafeError] = []
        try:
            if context is None:
                # Same outcome as the tools' "context" check: no trusted
                # identity, so nothing is retrieved.
                raise AccessDeniedError("context")
            evidence = retrieve_carrier_evidence(
                state["order_id"],
                runtime_context=context,
                adapter=carrier_adapter,
                clock=clock,
                freshness_limit=freshness_limit,
            )
            reason = evidence["reason"]
            if evidence["verification"] is not DeliveryStatus.VERIFIED and reason is not None:
                # Unavailable, stale, malformed, no events, etc.: keep the
                # tool's reason code as error provenance.
                errors.append(make_safe_error(CARRIER_NODE, reason, source_system=SOURCE_CARRIER))
        except AccessDeniedError:
            evidence, error = _failure(CARRIER_NODE, SOURCE_CARRIER, DENIED, ACCESS_DENIED)
            errors.append(error)
        except OrderNotFoundError:
            evidence, error = _failure(CARRIER_NODE, SOURCE_CARRIER, NOT_FOUND, ORDER_NOT_FOUND)
            errors.append(error)
        except InvalidToolInputError:
            evidence, error = _failure(CARRIER_NODE, SOURCE_CARRIER, ERROR, INVALID_ORDER_ID)
            errors.append(error)
        except InvalidStateError:
            evidence, error = _failure(CARRIER_NODE, SOURCE_CARRIER, ERROR, INVALID_CARRIER_EVIDENCE)
            errors.append(error)
        except Exception:  # noqa: BLE001 - any fault means status unknown [Decision D15]
            evidence, error = _failure(CARRIER_NODE, SOURCE_CARRIER, ERROR, CARRIER_RETRIEVAL_ERROR)
            errors.append(error)

        # VERIFIED only if the trusted check agrees; never from the claim alone.
        status = effective_delivery_status(
            {**state, "carrier_evidence": evidence, "delivery_status": evidence["verification"]}
        )
        update: dict[str, Any] = {"carrier_evidence": evidence, "delivery_status": status}
        if errors:
            update["errors"] = errors
        return _checked(state, update)

    def analysis_node(state: InvestigationState) -> dict[str, Any]:
        # Reached only via ROUTE_TO_EVIDENCE; anything else is a contract
        # violation and stops the run.
        oms = _usable_oms_evidence(state)
        if oms is None:
            raise InvalidStateError("OMS analysis requires RETRIEVED OMS evidence")
        try:
            analysis = derive_oms_analysis(oms, analyzed_at=clock())
        except InvalidStateError:
            error = make_safe_error(ANALYSIS_NODE, INVALID_OMS_ANALYSIS, source_system=SOURCE_OMS)
        except Exception:  # noqa: BLE001 - any fault means no analysis, never a guess
            error = make_safe_error(ANALYSIS_NODE, OMS_ANALYSIS_ERROR, source_system=SOURCE_OMS)
        else:
            return _checked(state, {"oms_analysis": analysis})
        return _checked(state, {"oms_analysis": None, "errors": [error]})

    builder = StateGraph(InvestigationState, context_schema=InvestigationRuntimeContext)
    builder.add_node(OMS_NODE, oms_node)
    builder.add_node(CARRIER_NODE, carrier_node)
    builder.add_node(ANALYSIS_NODE, analysis_node)
    builder.add_node(JOIN_NODE, validate_merged_evidence)
    builder.add_edge(START, OMS_NODE)
    builder.add_conditional_edges(OMS_NODE, route_after_oms, OMS_ROUTES)
    # Explicit fan-in: the join waits for every listed branch to finish.
    builder.add_edge(list(PARALLEL_NODES), JOIN_NODE)
    builder.add_edge(JOIN_NODE, END)
    return builder.compile()

