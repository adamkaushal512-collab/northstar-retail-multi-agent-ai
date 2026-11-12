"""Fail-closed investigation thread registry (Step 3.6F.18).

[Proposal] Binds an investigation thread ID (for example a LangGraph
``thread_id``) to the tenant, order and requesting user that created it, so
that a later step can check whether the current user may use that thread
*before* touching any checkpoint. Architecture-neutral: it does not import
LangGraph or any orchestration alternative, and it can gate threads for A, B
or C alike (AA sections 2, 8).

Scope of this step:

- Registration takes a trusted ``AccessContext`` built by application code
  from the authenticated session [Decision D16; FR-AUTH-01, FR-AUTH-02],
  runs ``require_read`` for OMS read (user permission and service scope),
  and derives ``tenant_id`` and ``created_by`` from that context. The caller
  cannot supply ownership fields.
- Thread IDs and order IDs are validated; a duplicate registration is
  rejected and never overwrites the existing record.
- ``authorize_thread_access`` re-checks the *current* context: OMS read
  permission and service scope, tenant match, and an initial owner-only
  policy (``user_id == created_by``) [Proposal]. Delegation, team access and
  supervisor access are [Open] and are not implemented.
- Fail closed and generic: an unknown thread, another tenant's thread and
  another user's thread raise the same ``AccessDeniedError`` with the same
  ``check`` value, so a denial does not reveal whether the thread exists or
  who owns it (FR-AUTH-03).

Not in this step: the registry never reads, writes or lists checkpoint
state, and it does not implement checkpoint recovery, resume, persistence,
evidence refresh or audit events. Passing authorization does not make any
checkpointed evidence trusted; the resume rules in
``northstar.orchestration.multi_agent.state`` still apply.

Development only: ``InMemoryThreadRegistry`` keeps records in process
memory. It is not restart-safe, not shared between processes or hosts, and
loses every record on restart (after which all threads are denied, which is
fail-closed but not recoverable). A durable implementation is [Open]; it
should implement the ``ThreadRegistry`` protocol with the same semantics,
including atomic insert-if-absent for duplicate rejection.

No identities are logged by this module, and the record's ``repr`` omits
tenant and user identifiers.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Protocol

from northstar.security.access_context import (
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
    require_read,
)
from northstar.tools.order_details import ORDER_ID_PATTERN

# [Proposal] Opaque thread ID: 8 to 128 characters of letters, digits, "-"
# and "_". Thread IDs should be generated server-side and be unguessable
# (for example ``secrets.token_urlsafe``); generation is not part of this step.
THREAD_ID_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"
_THREAD_ID_RE = re.compile(THREAD_ID_PATTERN)
_ORDER_ID_RE = re.compile(ORDER_ID_PATTERN)

# ``AccessDeniedError.check`` for every thread-level denial (unknown thread,
# invalid ID, tenant mismatch, owner mismatch). One value, so callers cannot
# tell these cases apart.
THREAD_CHECK = "thread"


class InvalidThreadRegistrationError(ValueError):
    """Registration input is invalid. The message carries no identifiers."""


class ThreadAlreadyRegisteredError(ValueError):
    """The thread ID is already registered. The message carries no identifiers."""

    def __init__(self) -> None:
        super().__init__("thread is already registered")


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _valid_thread_id(value: object) -> bool:
    return isinstance(value, str) and _THREAD_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class ThreadRecord:
    """Immutable thread metadata. Ownership comes from a trusted context."""

    thread_id: str
    tenant_id: str = field(repr=False)
    order_id: str
    created_by: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_thread_id(self.thread_id):
            raise InvalidThreadRegistrationError("thread_id has an invalid format")
        if not (isinstance(self.order_id, str) and _ORDER_ID_RE.fullmatch(self.order_id)):
            raise InvalidThreadRegistrationError("order_id has an invalid format")
        if not (_non_empty_str(self.tenant_id) and _non_empty_str(self.created_by)):
            raise InvalidThreadRegistrationError("ownership fields are invalid")


class ThreadRegistry(Protocol):
    """Interface for in-memory and future durable registries."""

    def register_thread(
        self, thread_id: str, order_id: str, access_context: AccessContext
    ) -> ThreadRecord:
        """Register a new thread owned by ``access_context``; never overwrite."""
        ...

    def authorize_thread_access(
        self, thread_id: str, current_access_context: AccessContext
    ) -> ThreadRecord:
        """Return the record if the current context may use the thread; else raise."""
        ...


class InMemoryThreadRegistry:
    """Development-only, process-local registry. Not restart-safe."""

    def __init__(self) -> None:
        self._records: dict[str, ThreadRecord] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def register_thread(
        self, thread_id: str, order_id: str, access_context: AccessContext
    ) -> ThreadRecord:
        # Authorize first, so an unauthorized caller learns nothing else.
        context = require_read(access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
        record = ThreadRecord(
            thread_id=thread_id,
            tenant_id=context.tenant_id,
            order_id=order_id,
            created_by=context.user_id,
        )
        with self._lock:
            if record.thread_id in self._records:
                raise ThreadAlreadyRegisteredError()
            self._records[record.thread_id] = record
        return record

    def authorize_thread_access(
        self, thread_id: str, current_access_context: AccessContext
    ) -> ThreadRecord:
        # Current permission and service scope, re-checked on every access.
        context = require_read(current_access_context, OMS_ORDER_READ, OMS_READ_SCOPE)
        if not _valid_thread_id(thread_id):
            raise AccessDeniedError(THREAD_CHECK)
        with self._lock:
            record = self._records.get(thread_id)
        if record is None:
            raise AccessDeniedError(THREAD_CHECK)
        if record.tenant_id != context.tenant_id:
            raise AccessDeniedError(THREAD_CHECK)
        # [Proposal] Initial owner-only policy.
        if record.created_by != context.user_id:
            raise AccessDeniedError(THREAD_CHECK)
        return record
