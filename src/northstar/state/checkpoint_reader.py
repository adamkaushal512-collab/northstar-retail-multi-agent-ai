"""Authorized, read-only checkpoint access (Step 3.6F.21).

[Proposal] Gates checkpoint reads behind ``ThreadRegistry``: every read
first calls ``registry.authorize_thread_access`` with the *current* trusted
``AccessContext`` and only then calls ``graph.get_state`` or
``graph.get_state_history``. A denied request makes no checkpoint call at
all, so it cannot reveal checkpoint values, history, thread metadata or
identifiers. Denials are the registry's generic ``AccessDeniedError``,
unchanged (FR-AUTH-03), and the registry's owner-only policy [Proposal] and
fail-closed semantics apply as-is.

Inputs:

- ``thread_id`` and the current ``AccessContext`` only. The context is built
  by application code from the authenticated session [Decision D16;
  FR-AUTH-01, FR-AUTH-02]; the caller cannot pass ``tenant_id``, ``user_id``
  or ownership fields.
- The LangGraph config is built here from the *authorized* record's
  ``thread_id`` (``{"configurable": {"thread_id": ...}}``). There is no way to
  pass a ``checkpoint_id``, ``checkpoint_ns`` or other config override, so a
  read always targets the latest checkpoint (or the full history) of the
  default namespace of that one thread. Returned snapshots are checked to
  belong to that thread and namespace; anything else fails closed.
- Every returned snapshot, latest or history, must carry a ``checkpoint_id``
  that is a nonempty ``str`` (Step 3.6F.27). A missing, ``None``, empty or
  non-string ID fails closed; only the exact LangGraph no-checkpoint shape
  reads as "no checkpoint".

Architecture-neutral: this module does not import LangGraph. ``graph`` is any
object with the compiled-graph read methods (``get_state``,
``get_state_history``) and a non-``None`` ``checkpointer``, for example the
evidence graph built with ``build_evidence_graph(checkpointer=...)``.

Not in this step: resume, evidence refresh, checkpoint writes or deletion,
checkpoint selection by ID, durable persistence and audit events. Reading a
checkpoint does not make its evidence current or trusted: stored evidence
may be stale, and current status still comes only from the source systems
[Decision D11, D15]. The resume rules in
``northstar.orchestration.multi_agent.state`` still apply.

Production [Open]: both the thread registry and the checkpointer must be
durable and appropriately secured (restart-safe, shared consistently across
processes, encrypted at rest, tenant-isolated, least-privilege access to the
store itself, defined retention). ``InMemoryThreadRegistry`` and LangGraph's
``InMemorySaver`` are development only. Authorization here only gates this
API; anyone with direct access to the checkpoint store bypasses it.
"""

from __future__ import annotations

from typing import Any, Protocol

from northstar.security.access_context import AccessContext
from northstar.state.thread_registry import ThreadRegistry

# LangGraph's default (root) checkpoint namespace. Only this namespace is
# readable through this API.
_ROOT_NAMESPACE = ""


class CheckpointIntegrityError(RuntimeError):
    """A checkpoint read returned malformed data or data for another thread or namespace.

    The message carries no identifiers.
    """

    def __init__(self) -> None:
        super().__init__("checkpoint does not match the authorized thread")


class CheckpointedGraph(Protocol):
    """The compiled-graph read methods this service uses."""

    checkpointer: Any

    def get_state(self, config: dict[str, Any]) -> Any: ...

    def get_state_history(self, config: dict[str, Any]) -> Any: ...


def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _configurable(snapshot: Any) -> dict[str, Any]:
    config = getattr(snapshot, "config", None)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if not isinstance(configurable, dict):
        raise CheckpointIntegrityError()
    return configurable


def _check_snapshot(snapshot: Any, thread_id: str) -> Any:
    configurable = _configurable(snapshot)
    if configurable.get("thread_id") != thread_id:
        raise CheckpointIntegrityError()
    if configurable.get("checkpoint_ns", _ROOT_NAMESPACE) != _ROOT_NAMESPACE:
        raise CheckpointIntegrityError()
    return snapshot


def _check_checkpoint_id(snapshot: Any) -> Any:
    """Require a nonempty ``str`` ``checkpoint_id`` on a persisted checkpoint (Step 3.6F.27)."""
    checkpoint_id = _configurable(snapshot).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or checkpoint_id == "":
        raise CheckpointIntegrityError()
    return snapshot


def _is_empty_snapshot(snapshot: Any, thread_id: str) -> bool:
    """Match the exact snapshot ``get_state`` returns for a thread with no checkpoints.

    Shape observed with LangGraph 1.2.20 ``StateSnapshot`` (Step 3.6F.25): the
    config echoes only the requested ``thread_id`` and every other field is
    empty. Anything else without a ``checkpoint_id`` is malformed, not empty.
    """
    return (
        _configurable(snapshot) == {"thread_id": thread_id}
        and getattr(snapshot, "values", None) == {}
        and getattr(snapshot, "next", None) == ()
        and getattr(snapshot, "metadata", ()) is None
        and getattr(snapshot, "created_at", ()) is None
        and getattr(snapshot, "parent_config", ()) is None
        and getattr(snapshot, "tasks", None) == ()
        and getattr(snapshot, "interrupts", None) == ()
    )


class AuthorizedCheckpointReader:
    """Read-only checkpoint access, authorized per request via the registry."""

    def __init__(self, registry: ThreadRegistry, graph: CheckpointedGraph) -> None:
        if getattr(graph, "checkpointer", None) is None:
            # Wiring error, not an access decision: without a checkpointer
            # LangGraph has nothing to read.
            raise ValueError("graph must be compiled with a checkpointer")
        self._registry = registry
        self._graph = graph

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def _authorized_config(
        self, thread_id: str, current_access_context: AccessContext
    ) -> tuple[str, dict[str, Any]]:
        # Raises AccessDeniedError before any checkpoint API is touched.
        record = self._registry.authorize_thread_access(thread_id, current_access_context)
        return record.thread_id, _thread_config(record.thread_id)

    def get_latest_checkpoint(
        self, thread_id: str, current_access_context: AccessContext
    ) -> Any | None:
        """Return the latest ``StateSnapshot`` of the thread, or ``None`` if it has none."""
        authorized_id, config = self._authorized_config(thread_id, current_access_context)
        snapshot = self._graph.get_state(config)
        # Ownership checks run before the empty-thread check, so a foreign,
        # nested-namespace or malformed snapshot raises instead of reading as
        # "no checkpoint". The exact empty shape has no ``checkpoint_id`` key;
        # anything else must carry a valid one.
        _check_snapshot(snapshot, authorized_id)
        if _is_empty_snapshot(snapshot, authorized_id):
            return None
        return _check_checkpoint_id(snapshot)

    def get_checkpoint_history(
        self, thread_id: str, current_access_context: AccessContext
    ) -> tuple[Any, ...]:
        """Return the thread's ``StateSnapshot`` history, newest first.

        The history is fully read here, right after authorization, so no lazy
        enumeration outlives the authorization check.
        """
        authorized_id, config = self._authorized_config(thread_id, current_access_context)
        return tuple(
            _check_checkpoint_id(_check_snapshot(snapshot, authorized_id))
            for snapshot in self._graph.get_state_history(config)
        )
