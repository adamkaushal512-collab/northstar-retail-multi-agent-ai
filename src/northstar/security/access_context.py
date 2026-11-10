"""Minimal synthetic access context and fail-closed authorization (Step 2.6B).

[Assumption] No identity provider exists yet. ``AccessContext`` is a
synthetic stand-in for an authenticated requesting user plus the platform's
service scope [Decision D16; FR-AUTH-01, FR-AUTH-02]. It is built by trusted
application code, never from model-supplied tool arguments, so the model
cannot choose which user, tenant or customer it acts for.

Fail closed: a missing, malformed or incomplete context is denied, and both
the user permission and the service scope must allow the read. Denials do
not reveal restricted content (FR-AUTH-03). The platform is never an
approver [Decision D18]; this module only grants or denies reads.

Not implemented here (see FR-AUTH-02 AC1, FR-AUTH-03 AC1): audit events such
as ``authorization.checked`` and ``authorization.denied``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Read-only permission names. [Proposal] Naming is illustrative only.
OMS_ORDER_READ = "oms:order:read"  # requesting user's permission
OMS_READ_SCOPE = "oms:read"  # platform service scope (least privilege)
CARRIER_TRACKING_READ = "carrier:tracking:read"  # requesting user's permission (Step 2.6C)
CARRIER_READ_SCOPE = "carrier:read"  # platform service scope (Step 2.6C)

DENIED_MESSAGE = "not accessible to requesting user"


class AccessDeniedError(PermissionError):
    """Raised when a read is not authorized. Carries no restricted content."""

    def __init__(self, check: str) -> None:
        # ``check`` names which check failed (user, service or context), never data.
        super().__init__(DENIED_MESSAGE)
        self.check = check


@dataclass(frozen=True)
class AccessContext:
    """Trusted, immutable description of who is asking and in which tenant."""

    user_id: str
    tenant_id: str
    user_permissions: frozenset[str] = field(default_factory=frozenset)
    service_scopes: frozenset[str] = field(default_factory=frozenset)


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def require_read(context: object, user_permission: str, service_scope: str) -> AccessContext:
    """Return the context if both checks allow the read; otherwise raise.

    Called before any retrieval, so a denied request retrieves nothing.
    """
    if not isinstance(context, AccessContext):
        raise AccessDeniedError("context")
    if not (_non_empty_str(context.user_id) and _non_empty_str(context.tenant_id)):
        raise AccessDeniedError("context")
    if not isinstance(context.user_permissions, frozenset) or not isinstance(
        context.service_scopes, frozenset
    ):
        raise AccessDeniedError("context")
    if user_permission not in context.user_permissions:
        raise AccessDeniedError("user")
    if service_scope not in context.service_scopes:
        raise AccessDeniedError("service")
    return context
