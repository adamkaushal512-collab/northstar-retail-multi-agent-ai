"""Authorized investigation refresh (Step 3.6F.28D).

[Proposal] Starts a *fresh* investigation for the order of an existing,
authorized thread. This is a refresh/restart, not a LangGraph resume: the
saved checkpoint is used only to confirm that the thread has a well-formed
investigation for the authorized order. Nothing else from it is reused.
There is no public path that resumes a checkpoint mid-graph
(``graph.invoke(None, ...)``), and no checkpoint ID, namespace, execution
thread ID, tenant or ownership field can be passed in.

Inputs: the original ``thread_id`` and the current trusted ``AccessContext``
only. The context is built by application code from the authenticated
session [Decision D16; FR-AUTH-01, FR-AUTH-02].

Order of operations (each step fails closed before the next one runs):

1. Reauthorize the original thread through ``ThreadRegistry`` (current OMS
   permission and scope, tenant match, owner-only policy [Proposal]).
2. Require the current OMS *and* carrier read permissions and service
   scopes. A refresh needs both retrievals, so a context that cannot read
   carrier data is denied before any checkpoint read or execution.
3. Read the latest checkpoint through an ``AuthorizedCheckpointReader``
   (which reauthorizes again and checks thread, namespace and checkpoint
   ID). The service builds this reader itself from the same trusted
   registry and checkpointed graph it executes with (Step 3.6F.28E), so the
   checkpoint read and the fresh run cannot be wired to different
   registries, graphs or checkpointers. Callers cannot inject a reader.
4. Reject a missing checkpoint, and run a *minimal* identity and shape check
   on the checkpoint values: a mapping with exactly the
   ``InvestigationState`` keys and an ``order_id`` equal to the authorized
   registry record's. This is not a full validation. ``validate_state`` is
   deliberately *not* run on saved values: checkpointed VERIFIED evidence
   from another process no longer attests and would be rejected, which is
   exactly the stale case a refresh exists to replace (state module,
   "Checkpoint resume"). Historical evidence, analysis, errors and status
   in the checkpoint are therefore *not validated*; they are discarded
   unread and never trusted, copied or returned.
5. Generate a new thread ID with ``secrets.token_urlsafe`` and check it
   against ``THREAD_ID_PATTERN``.
6. Register the new thread for the authorized order, owned by the current
   context.
7. Run the evidence graph from ``initial_state(order_id)`` with a newly
   built ``InvestigationRuntimeContext``, so OMS and carrier evidence are
   retrieved again under the current user's permissions [Decision D11, D15].
8. Validate the returned state (``validate_state``) and its ``order_id``.
9. Return the new thread ID and the validated state.

Saved evidence, analysis, errors, delivery status and final investigation
are never copied into the new run. The original thread and its checkpoints
are not modified.

Errors are generic: authorization failures are the unchanged
``AccessDeniedError`` from the registry or ``require_read``; checkpoint
problems raise ``MissingCheckpointError`` or ``CheckpointIntegrityError``;
any execution or result-validation failure raises
``InvestigationRefreshError``. No message carries tenant or user
identifiers, checkpoint content or exception text.

Not atomic [Open]: registration (step 6) and graph execution (step 7) are
separate operations. If execution fails, the new thread stays registered
and may hold incomplete checkpoints. The caller receives no thread ID in
that case. Cleanup or a transactional design is a later step.

Read-only: no retries, refunds, reships, order changes, customer messages or
other write operations [Decision D5]. ``InMemoryThreadRegistry`` and
in-memory checkpointers are development only; durable storage is [Open].
This module does not import LangGraph.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from northstar.orchestration.multi_agent.state import (
    InvalidStateError,
    InvestigationRuntimeContext,
    InvestigationState,
    initial_state,
    validate_state,
)
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    require_read,
)
from northstar.state.checkpoint_reader import (
    AuthorizedCheckpointReader,
    CheckpointedGraph,
    CheckpointIntegrityError,
)
from northstar.state.thread_registry import THREAD_ID_PATTERN, ThreadRegistry

_THREAD_ID_RE = re.compile(THREAD_ID_PATTERN)
# 32 random bytes -> 43 URL-safe characters, within THREAD_ID_PATTERN.
_THREAD_ID_BYTES = 32
_STATE_KEYS = frozenset(InvestigationState.__annotations__)


class MissingCheckpointError(LookupError):
    """The authorized thread has no checkpoint to refresh from. No identifiers."""

    def __init__(self) -> None:
        super().__init__("thread has no checkpoint")


class InvestigationRefreshError(RuntimeError):
    """The refreshed investigation did not complete. No identifiers or causes."""

    def __init__(self) -> None:
        super().__init__("investigation refresh failed")


class InvokableGraph(CheckpointedGraph, Protocol):
    """A checkpointed compiled graph: read methods plus ``invoke`` for the fresh run."""

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class InvestigationRefreshResult:
    """The new thread and its validated investigation state."""

    thread_id: str
    state: InvestigationState = field(repr=False)


def _new_thread_id(original_thread_id: str) -> str:
    thread_id = secrets.token_urlsafe(_THREAD_ID_BYTES)
    if _THREAD_ID_RE.fullmatch(thread_id) is None or thread_id == original_thread_id:
        raise InvestigationRefreshError()
    return thread_id


def _check_checkpoint_values(values: object, order_id: str) -> None:
    """Shape and order check only; saved evidence is discarded, never trusted."""
    if not isinstance(values, Mapping) or set(values) != _STATE_KEYS:
        raise CheckpointIntegrityError()
    if values["order_id"] != order_id:
        raise CheckpointIntegrityError()


class AuthorizedInvestigationRefreshService:
    """Fresh, authorized re-run of an existing thread's investigation."""

    def __init__(self, registry: ThreadRegistry, graph: InvokableGraph) -> None:
        # Wiring error, not an access decision.
        if getattr(graph, "checkpointer", None) is None:
            raise ValueError("graph must be compiled with a checkpointer")
        self._registry = registry
        self._graph = graph
        # Built here, never injected: the checkpoint read uses exactly the
        # registry and graph (and so the checkpointer) used for execution.
        self._reader = AuthorizedCheckpointReader(registry, graph)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def refresh(
        self, thread_id: str, current_access_context: AccessContext
    ) -> InvestigationRefreshResult:
        """Run a fresh investigation for the authorized thread's order on a new thread."""
        # 1. Thread access (raises the registry's generic AccessDeniedError).
        record = self._registry.authorize_thread_access(thread_id, current_access_context)
        # 2. Both retrievals must be allowed before any checkpoint read or run.
        context = require_read(current_access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
        require_read(context, CARRIER_TRACKING_READ, CARRIER_READ_SCOPE)

        # 3-4. Latest checkpoint of the authorized thread, shape and order only.
        snapshot = self._reader.get_latest_checkpoint(record.thread_id, context)
        if snapshot is None:
            raise MissingCheckpointError()
        _check_checkpoint_values(getattr(snapshot, "values", None), record.order_id)

        # 5-6. New unguessable thread, owned by the current context.
        new_thread_id = _new_thread_id(record.thread_id)
        new_record = self._registry.register_thread(new_thread_id, record.order_id, context)

        # 7. Fresh run: nothing from the saved checkpoint enters the input.
        try:
            result = self._graph.invoke(
                initial_state(new_record.order_id),
                config={"configurable": {"thread_id": new_record.thread_id}},
                context=InvestigationRuntimeContext(access_context=context),
            )
            # 8. Validate the returned state and its order.
            state = validate_state(result)
            if state["order_id"] != new_record.order_id:
                raise InvalidStateError("result is for a different order")
        except Exception:  # noqa: BLE001 - never report a partial run as success
            raise InvestigationRefreshError() from None

        # 9. Minimal result.
        return InvestigationRefreshResult(thread_id=new_record.thread_id, state=state)
