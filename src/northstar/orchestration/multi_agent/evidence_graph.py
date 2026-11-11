"""Sequential OMS -> carrier evidence graph (Step 3.3).

[Proposal] LangGraph is a candidate only (ADR-002 Proposed). This module is
part of the LangGraph hands-on implementation used to benchmark the B and C
candidates (ADR-002 section 3, AA section 8). It does not select an
architecture or an agent count: it has no model, coordinator or specialist
nodes, only two deterministic evidence-retrieval nodes over the Phase 2
read-only tools. Requires the optional ``langgraph`` extra.

Graph: START -> retrieve_oms_evidence -> retrieve_carrier_evidence -> END.
No conditional routing, parallel branches, checkpointer, recovery or human
approval steps (later steps; human steps stay outside LangGraph, ADR-002
section 3.4).

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

[Open] ``SafeError.retryable`` is always false here; retry classification
belongs to recovery (later step) and NFR retry targets (Step 1.4).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

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
NODE_ORDER = (OMS_NODE, CARRIER_NODE)

# Value-free error codes. [Proposal] Names are illustrative.
ACCESS_DENIED = "ACCESS_DENIED"
ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
INVALID_ORDER_ID = "INVALID_ORDER_ID"
INVALID_OMS_EVIDENCE = "INVALID_OMS_EVIDENCE"
INVALID_CARRIER_EVIDENCE = "INVALID_CARRIER_EVIDENCE"
OMS_RETRIEVAL_ERROR = "OMS_RETRIEVAL_ERROR"
CARRIER_RETRIEVAL_ERROR = "CARRIER_RETRIEVAL_ERROR"


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


def build_evidence_graph(
    *,
    carrier_adapter: CarrierTrackingSource | None = None,
    clock: Clock = utc_now,
    freshness_limit: timedelta = SYNTHETIC_FRESHNESS_LIMIT,
):
    """Compile the sequential evidence graph.

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

    builder = StateGraph(InvestigationState, context_schema=InvestigationRuntimeContext)
    builder.add_node(OMS_NODE, oms_node)
    builder.add_node(CARRIER_NODE, carrier_node)
    builder.add_edge(START, OMS_NODE)
    builder.add_edge(OMS_NODE, CARRIER_NODE)
    builder.add_edge(CARRIER_NODE, END)
    return builder.compile()

