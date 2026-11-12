"""Unit tests for the fail-closed investigation thread registry (Step 3.6F.18).

Synthetic data only. No LangGraph, AWS, Bedrock or network calls. The
checkpoint saver used below is a ``mock.Mock`` that stands in for any saver
a later resume step might gate behind the registry.
"""

import dataclasses
import os
import subprocess
import sys
import unittest
from unittest import mock

from northstar.security.access_context import (
    DENIED_MESSAGE,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
)
from northstar.state.thread_registry import (
    THREAD_CHECK,
    InMemoryThreadRegistry,
    InvalidThreadRegistrationError,
    ThreadAlreadyRegisteredError,
    ThreadRecord,
)

THREAD_ID = "thread-SYN-0001-abcdef"
ORDER_ID = "SYN-ORD-0001"


def context(user_id="SYN-USER-0001", tenant_id="SYN-TENANT-A", perms=None, scopes=None):
    return AccessContext(
        user_id,
        tenant_id,
        frozenset({OMS_ORDER_READ}) if perms is None else perms,
        frozenset({OMS_READ_SCOPE}) if scopes is None else scopes,
    )


OWNER = context()
SAME_TENANT_OTHER_USER = context(user_id="SYN-USER-0002")
OTHER_TENANT_SAME_USER_ID = context(tenant_id="SYN-TENANT-B")


def gated_checkpoint_read(registry, saver, thread_id, access_context):
    """Pattern a later resume step must follow: authorize, then read."""
    registry.authorize_thread_access(thread_id, access_context)
    return saver.get_tuple({"configurable": {"thread_id": thread_id}})


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = InMemoryThreadRegistry()

    def test_ownership_derived_from_trusted_context(self):
        record = self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        self.assertEqual(record.thread_id, THREAD_ID)
        self.assertEqual(record.order_id, ORDER_ID)
        self.assertEqual(record.tenant_id, OWNER.tenant_id)
        self.assertEqual(record.created_by, OWNER.user_id)

    def test_caller_cannot_supply_ownership_fields(self):
        for kwarg in ({"tenant_id": "SYN-TENANT-B"}, {"created_by": "SYN-USER-0002"}):
            with self.subTest(kwarg=kwarg), self.assertRaises(TypeError):
                self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER, **kwarg)
        with self.assertRaises(AccessDeniedError):
            self.registry.authorize_thread_access(THREAD_ID, OWNER)

    def test_record_is_immutable(self):
        record = self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        for name in ("thread_id", "tenant_id", "order_id", "created_by"):
            with self.subTest(field=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "SYN-USER-0002")

    def test_record_repr_omits_identities(self):
        record = self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        self.assertNotIn(OWNER.user_id, repr(record))
        self.assertNotIn(OWNER.tenant_id, repr(record))
        self.assertNotIn(THREAD_ID, repr(self.registry))

    def test_invalid_thread_ids_rejected(self):
        for bad in (None, "", "   ", "short", 123, "has space-in-id", "x" * 129, "a/b/c/d/e/f"):
            with self.subTest(thread_id=bad), self.assertRaises(InvalidThreadRegistrationError):
                self.registry.register_thread(bad, ORDER_ID, OWNER)

    def test_invalid_order_ids_rejected(self):
        for bad in (None, "", "ORD-1", "SYN-ORD-1", 1):
            with self.subTest(order_id=bad), self.assertRaises(InvalidThreadRegistrationError):
                self.registry.register_thread(THREAD_ID, bad, OWNER)
        with self.assertRaises(AccessDeniedError):
            self.registry.authorize_thread_access(THREAD_ID, OWNER)

    def test_duplicate_registration_rejected_without_overwrite(self):
        original = self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        attempts = (
            (ORDER_ID, OWNER),
            ("SYN-ORD-0002", OWNER),
            (ORDER_ID, SAME_TENANT_OTHER_USER),
            (ORDER_ID, OTHER_TENANT_SAME_USER_ID),
        )
        for order_id, ctx in attempts:
            with self.subTest(order_id=order_id, user=ctx.user_id, tenant=ctx.tenant_id):
                with self.assertRaises(ThreadAlreadyRegisteredError) as raised:
                    self.registry.register_thread(THREAD_ID, order_id, ctx)
                self.assertNotIn(THREAD_ID, str(raised.exception))
        self.assertEqual(self.registry.authorize_thread_access(THREAD_ID, OWNER), original)
        with self.assertRaises(AccessDeniedError):
            self.registry.authorize_thread_access(THREAD_ID, SAME_TENANT_OTHER_USER)

    def test_unauthorized_registration_stores_nothing(self):
        denied = (
            context(perms=frozenset()),
            context(scopes=frozenset()),
            None,
            {"user_id": OWNER.user_id, "tenant_id": OWNER.tenant_id},
            context(user_id=""),
            context(tenant_id="  "),
        )
        for ctx in denied:
            with self.subTest(context=type(ctx).__name__), self.assertRaises(AccessDeniedError):
                self.registry.register_thread(THREAD_ID, ORDER_ID, ctx)
        # The thread ID is still free for the authorized owner.
        self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)

    def test_authorization_checked_before_input_validation(self):
        with self.assertRaises(AccessDeniedError):
            self.registry.register_thread("bad id", "bad", context(perms=frozenset()))


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.registry = InMemoryThreadRegistry()
        self.record = self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)

    def assert_generic_denial(self, thread_id, ctx, check=THREAD_CHECK):
        with self.assertRaises(AccessDeniedError) as raised:
            self.registry.authorize_thread_access(thread_id, ctx)
        err = raised.exception
        self.assertEqual(str(err), DENIED_MESSAGE)
        self.assertEqual(err.check, check)
        text = f"{err!s} {err!r} {err.args!r}"
        for secret in (THREAD_ID, ORDER_ID, OWNER.user_id, OWNER.tenant_id):
            self.assertNotIn(secret, text)

    def test_authorized_owner(self):
        self.assertEqual(self.registry.authorize_thread_access(THREAD_ID, OWNER), self.record)

    def test_owner_with_fresh_equivalent_context(self):
        # A newly built context for the same user and tenant is accepted.
        fresh = context(perms=frozenset({OMS_ORDER_READ, "carrier:tracking:read"}))
        self.assertEqual(self.registry.authorize_thread_access(THREAD_ID, fresh), self.record)

    def test_other_user_same_tenant_denied(self):
        self.assert_generic_denial(THREAD_ID, SAME_TENANT_OTHER_USER)

    def test_other_tenant_denied(self):
        self.assert_generic_denial(THREAD_ID, OTHER_TENANT_SAME_USER_ID)
        self.assert_generic_denial(THREAD_ID, context(user_id="SYN-USER-0009", tenant_id="SYN-TENANT-B"))

    def test_unknown_thread_denied_like_mismatch(self):
        self.assert_generic_denial("thread-unknown-000000", OWNER)

    def test_invalid_thread_id_denied(self):
        for bad in (None, "", "short", "bad id with spaces", 42):
            with self.subTest(thread_id=bad):
                self.assert_generic_denial(bad, OWNER)

    def test_missing_or_revoked_user_permission_denied(self):
        self.assert_generic_denial(THREAD_ID, context(perms=frozenset()), check="user")
        self.assert_generic_denial(
            THREAD_ID, context(perms=frozenset({"carrier:tracking:read"})), check="user"
        )

    def test_missing_or_revoked_service_scope_denied(self):
        self.assert_generic_denial(THREAD_ID, context(scopes=frozenset()), check="service")

    def test_revocation_takes_effect_on_next_access(self):
        self.registry.authorize_thread_access(THREAD_ID, OWNER)
        self.assert_generic_denial(THREAD_ID, context(perms=frozenset()), check="user")

    def test_malformed_context_denied(self):
        malformed = (
            None,
            "SYN-USER-0001",
            {"user_id": OWNER.user_id, "tenant_id": OWNER.tenant_id},
            context(user_id=""),
            context(user_id="   "),
            context(tenant_id=""),
            AccessContext(OWNER.user_id, OWNER.tenant_id, {OMS_ORDER_READ}, frozenset({OMS_READ_SCOPE})),
        )
        for ctx in malformed:
            with self.subTest(context=repr(type(ctx))):
                self.assert_generic_denial(THREAD_ID, ctx, check="context")

    def test_permission_checked_before_lookup(self):
        # An unauthorized caller gets the same answer for known and unknown threads.
        for thread_id in (THREAD_ID, "thread-unknown-000000"):
            with self.subTest(thread_id=thread_id):
                self.assert_generic_denial(thread_id, context(perms=frozenset()), check="user")


class NoCheckpointAccessTests(unittest.TestCase):
    def setUp(self):
        self.registry = InMemoryThreadRegistry()
        self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)

    def test_no_checkpoint_access_on_denial(self):
        denied = (
            (THREAD_ID, SAME_TENANT_OTHER_USER),
            (THREAD_ID, OTHER_TENANT_SAME_USER_ID),
            (THREAD_ID, context(perms=frozenset())),
            (THREAD_ID, context(scopes=frozenset())),
            (THREAD_ID, None),
            ("thread-unknown-000000", OWNER),
        )
        for thread_id, ctx in denied:
            saver = mock.Mock()
            with self.subTest(thread_id=thread_id, context=repr(type(ctx))):
                with self.assertRaises(AccessDeniedError):
                    gated_checkpoint_read(self.registry, saver, thread_id, ctx)
                self.assertEqual(saver.mock_calls, [])

    def test_owner_reaches_checkpoint_only_after_authorization(self):
        saver = mock.Mock()
        gated_checkpoint_read(self.registry, saver, THREAD_ID, OWNER)
        saver.get_tuple.assert_called_once_with({"configurable": {"thread_id": THREAD_ID}})

    def test_registry_does_not_import_langgraph(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        code = (
            "import sys\n"
            "import northstar.state.thread_registry\n"
            "loaded = [m for m in sys.modules if m.split('.')[0] == 'langgraph'\n"
            "          or m.startswith('northstar.orchestration')]\n"
            "sys.exit(1 if loaded else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": src},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ThreadRecordTests(unittest.TestCase):
    def test_direct_record_validates_fields(self):
        with self.assertRaises(InvalidThreadRegistrationError):
            ThreadRecord(THREAD_ID, "", ORDER_ID, OWNER.user_id)
        with self.assertRaises(InvalidThreadRegistrationError):
            ThreadRecord(THREAD_ID, OWNER.tenant_id, ORDER_ID, None)


if __name__ == "__main__":
    unittest.main()
