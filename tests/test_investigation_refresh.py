"""Unit tests for the authorized investigation refresh (Step 3.6F.28D).

Uses the real evidence graph with a real LangGraph ``InMemorySaver``, the
in-memory thread registry and ``mock`` spies. Synthetic data only. No AWS,
Bedrock, carrier or network calls.
"""

import ast
import importlib.util
import inspect
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from northstar.evidence.investigation_schema import DeliveryStatus
from northstar.integrations.synthetic_carrier import SyntheticCarrierAdapter
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    DENIED_MESSAGE,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
)
from northstar.state.checkpoint_reader import AuthorizedCheckpointReader, CheckpointIntegrityError
from northstar.state.thread_registry import THREAD_CHECK, THREAD_ID_PATTERN, InMemoryThreadRegistry

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
if HAS_LANGGRAPH:
    from langgraph.checkpoint.memory import InMemorySaver

    from northstar.orchestration.multi_agent import evidence_graph as eg
    from northstar.orchestration.multi_agent import state as st
    from northstar.orchestration.multi_agent.state import InvestigationRuntimeContext, initial_state
    from northstar.state import investigation_refresh as ir
    from northstar.state.investigation_refresh import (
        AuthorizedInvestigationRefreshService,
        InvestigationRefreshError,
        InvestigationRefreshResult,
        MissingCheckpointError,
    )

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
THREAD_ID = "thread-SYN-0001-abcdef"
EMPTY_THREAD_ID = "thread-SYN-0009-abcdef"
UNKNOWN_THREAD_ID = "thread-unknown-000000"
ORDER_ID = "SYN-ORD-0004"
OTHER_ORDER_ID = "SYN-ORD-0001"
SECRET_TEXT = "SYN-SECRET-should-not-leak"

USER_ID = "SYN-USER-0001"
TENANT_ID = "SYN-TENANT-A"
ALL_PERMS = frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ})
ALL_SCOPES = frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE})


def context(user_id=USER_ID, tenant_id=TENANT_ID, perms=ALL_PERMS, scopes=ALL_SCOPES):
    return AccessContext(user_id, tenant_id, perms, scopes)


OWNER = context()
OMS_ONLY_OWNER = context(perms=frozenset({OMS_ORDER_READ}), scopes=frozenset({OMS_READ_SCOPE}))

# (name, thread_id, context, expected AccessDeniedError.check)
DENIED_CASES = (
    ("wrong_tenant", THREAD_ID, context(tenant_id="SYN-TENANT-B"), THREAD_CHECK),
    ("wrong_owner", THREAD_ID, context(user_id="SYN-USER-0002"), THREAD_CHECK),
    ("unknown_thread", UNKNOWN_THREAD_ID, OWNER, THREAD_CHECK),
    ("invalid_thread_id", "bad id", OWNER, THREAD_CHECK),
    ("revoked_oms_permission", THREAD_ID, context(perms=frozenset({CARRIER_TRACKING_READ})), "user"),
    ("revoked_oms_scope", THREAD_ID, context(scopes=frozenset({CARRIER_READ_SCOPE})), "service"),
    ("revoked_carrier_permission", THREAD_ID, context(perms=frozenset({OMS_ORDER_READ})), "user"),
    ("revoked_carrier_scope", THREAD_ID, context(scopes=frozenset({OMS_READ_SCOPE})), "service"),
    ("missing_context", THREAD_ID, None, "context"),
    (
        "untrusted_context_mapping",
        THREAD_ID,
        {"user_id": USER_ID, "tenant_id": TENANT_ID, "user_permissions": ALL_PERMS},
        "context",
    ),
)


def config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class InvestigationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        clock = lambda: self.now  # noqa: E731 - advanced by tests
        self.saver = InMemorySaver()
        self.graph = eg.build_evidence_graph(
            clock=clock,
            carrier_adapter=SyntheticCarrierAdapter(clock=clock),
            checkpointer=self.saver,
        )
        self.registry = InMemoryThreadRegistry()
        self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        # Original run without carrier access: its checkpoint holds DENIED
        # carrier evidence, an UNVERIFIED status and a SafeError.
        self.original = self.graph.invoke(
            initial_state(ORDER_ID),
            context=InvestigationRuntimeContext(access_context=OMS_ONLY_OWNER),
            config=config(THREAD_ID),
        )
        # Test-side reader for inspecting checkpoints; the service builds its own.
        self.reader = AuthorizedCheckpointReader(self.registry, self.graph)
        self.service = AuthorizedInvestigationRefreshService(self.registry, self.graph)

    def spies(self):
        """Call-through spies on every checkpoint, registry and execution path."""
        targets = {
            "get_state": (self.graph, "get_state"),
            "get_tuple": (self.saver, "get_tuple"),
            "invoke": (self.graph, "invoke"),
            "register": (self.registry, "register_thread"),
            "oms": (eg, "get_order_details"),
            "carrier": (eg, "retrieve_carrier_evidence"),
        }
        mocks = {}
        for name, (obj, attr) in targets.items():
            patcher = mock.patch.object(obj, attr, wraps=getattr(obj, attr))
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return mocks

    def assert_nothing_after_denial(self, spies):
        for name in ("get_state", "get_tuple", "invoke", "register", "oms", "carrier"):
            spies[name].assert_not_called()

    def assert_no_registration_or_execution(self, spies):
        for name in ("invoke", "register", "oms", "carrier"):
            spies[name].assert_not_called()

    def history(self, thread_id):
        return [
            (s.config["configurable"]["checkpoint_id"], s.values)
            for s in self.reader.get_checkpoint_history(thread_id, OWNER)
        ]

    # Successful refresh -----------------------------------------------

    def test_successful_refresh_returns_minimal_result(self):
        self.now = LATER
        result = self.service.refresh(THREAD_ID, OWNER)
        self.assertIsInstance(result, InvestigationRefreshResult)
        self.assertEqual(set(vars(result)), {"thread_id", "state"})
        st.validate_state(result.state)
        self.assertEqual(result.state["order_id"], ORDER_ID)
        self.assertIs(result.state["delivery_status"], DeliveryStatus.VERIFIED)
        self.assertIs(result.state["requires_human_review"], True)
        self.assertEqual(result.state["errors"], [])

    def test_new_thread_id_differs_and_is_unguessable_format(self):
        first = self.service.refresh(THREAD_ID, OWNER)
        second = self.service.refresh(THREAD_ID, OWNER)
        self.assertNotEqual(first.thread_id, THREAD_ID)
        self.assertNotEqual(first.thread_id, second.thread_id)
        self.assertRegex(first.thread_id, THREAD_ID_PATTERN)
        self.assertGreaterEqual(len(first.thread_id), 43)

    def test_fresh_oms_and_carrier_retrieval(self):
        spies = self.spies()
        self.now = LATER
        result = self.service.refresh(THREAD_ID, OWNER)
        spies["oms"].assert_called_once()
        spies["carrier"].assert_called_once()
        self.assertEqual(spies["oms"].call_args.args[0], ORDER_ID)
        self.assertIs(spies["oms"].call_args.kwargs["access_context"], OWNER)
        runtime = spies["carrier"].call_args.kwargs["runtime_context"]
        self.assertIsInstance(runtime, InvestigationRuntimeContext)
        self.assertIs(runtime.access_context, OWNER)
        self.assertEqual(result.state["oms_evidence"]["retrieved_at"], LATER.isoformat())
        self.assertEqual(result.state["carrier_evidence"]["retrieval_status"], st.RETRIEVED)

    def test_execution_starts_from_initial_state_on_new_thread(self):
        spies = self.spies()
        result = self.service.refresh(THREAD_ID, OWNER)
        spies["invoke"].assert_called_once()
        call = spies["invoke"].call_args
        self.assertEqual(call.args[0], initial_state(ORDER_ID))
        self.assertEqual(call.kwargs["config"], config(result.thread_id))
        self.assertIsInstance(call.kwargs["context"], InvestigationRuntimeContext)
        self.assertIs(call.kwargs["context"].access_context, OWNER)

    def test_old_checkpoint_evidence_and_errors_not_copied(self):
        self.assertEqual(self.original["carrier_evidence"]["retrieval_status"], st.DENIED)
        self.assertTrue(self.original["errors"])
        self.now = LATER
        result = self.service.refresh(THREAD_ID, OWNER)
        self.assertEqual(result.state["errors"], [])
        self.assertNotEqual(result.state["carrier_evidence"], self.original["carrier_evidence"])
        self.assertNotEqual(result.state["oms_evidence"], self.original["oms_evidence"])
        self.assertNotEqual(result.state["oms_analysis"], self.original["oms_analysis"])
        self.assertIsNone(result.state["final_investigation"])
        # The new thread's first state is exactly the fresh initial input.
        history = self.reader.get_checkpoint_history(result.thread_id, OWNER)
        self.assertEqual(history[-1].values, {"errors": []})
        self.assertEqual(history[-2].values, initial_state(ORDER_ID))

    def test_original_checkpoint_unchanged(self):
        before = self.history(THREAD_ID)
        result = self.service.refresh(THREAD_ID, OWNER)
        self.assertEqual(self.history(THREAD_ID), before)
        latest = self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
        self.assertEqual(latest.values, self.original)
        self.assertNotEqual(self.history(result.thread_id), before)

    def test_result_and_thread_bound_to_authorized_order_and_owner(self):
        result = self.service.refresh(THREAD_ID, OWNER)
        record = self.registry.authorize_thread_access(result.thread_id, OWNER)
        self.assertEqual(record.order_id, ORDER_ID)
        self.assertEqual(result.state["order_id"], record.order_id)
        latest = self.reader.get_latest_checkpoint(result.thread_id, OWNER)
        self.assertEqual(latest.values, result.state)
        for name, _, ctx, _ in DENIED_CASES[:2]:
            with self.subTest(name), self.assertRaises(AccessDeniedError):
                self.registry.authorize_thread_access(result.thread_id, ctx)

    def test_refresh_of_refreshed_thread(self):
        first = self.service.refresh(THREAD_ID, OWNER)
        second = self.service.refresh(first.thread_id, OWNER)
        self.assertNotIn(second.thread_id, (THREAD_ID, first.thread_id))
        self.assertEqual(second.state["order_id"], ORDER_ID)

    # Denials ----------------------------------------------------------

    def test_denied_contexts_touch_nothing(self):
        for name, thread_id, ctx, check in DENIED_CASES:
            with self.subTest(name):
                spies = self.spies()
                with self.assertRaises(AccessDeniedError) as raised:
                    self.service.refresh(thread_id, ctx)
                self.assertEqual(raised.exception.check, check)
                self.assertEqual(str(raised.exception), DENIED_MESSAGE)
                self.assert_nothing_after_denial(spies)
                mock.patch.stopall()

    def test_denial_messages_reveal_no_identifiers(self):
        for name, thread_id, ctx, _ in DENIED_CASES:
            with self.subTest(name), self.assertRaises(AccessDeniedError) as raised:
                self.service.refresh(thread_id, ctx)
            text = str(raised.exception) + repr(raised.exception.args)
            for value in (USER_ID, TENANT_ID, "SYN-TENANT-B", "SYN-USER-0002", ORDER_ID, THREAD_ID):
                self.assertNotIn(value, text)

    # Checkpoint rejection ---------------------------------------------

    def test_missing_checkpoint_rejected(self):
        self.registry.register_thread(EMPTY_THREAD_ID, ORDER_ID, OWNER)
        spies = self.spies()
        with self.assertRaises(MissingCheckpointError):
            self.service.refresh(EMPTY_THREAD_ID, OWNER)
        self.assert_no_registration_or_execution(spies)

    def patch_latest(self, **replacements):
        snapshot = self.graph.get_state(config(THREAD_ID))
        patcher = mock.patch.object(
            self.graph, "get_state", return_value=snapshot._replace(**replacements)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_checkpoint_rejected(self):
        spies = self.spies()
        with self.assertRaises(CheckpointIntegrityError) as raised:
            self.service.refresh(THREAD_ID, OWNER)
        self.assertNotIn(ORDER_ID, str(raised.exception))
        self.assert_no_registration_or_execution(spies)

    def test_malformed_checkpoint_values_rejected(self):
        cases = {
            "not_a_mapping": ["not", "a", "state"],
            "none": None,
            "missing_keys": {"order_id": ORDER_ID},
            "extra_key": {**self.original, "access_context": "x"},
        }
        for name, values in cases.items():
            with self.subTest(name):
                self.patch_latest(values=values)
                self.assert_checkpoint_rejected()
                mock.patch.stopall()

    def test_malformed_checkpoint_metadata_rejected(self):
        snapshot = self.graph.get_state(config(THREAD_ID))
        configurable = snapshot.config["configurable"]
        cases = {
            "missing_checkpoint_id": {"thread_id": THREAD_ID, "checkpoint_ns": ""},
            "foreign_thread": {**configurable, "thread_id": "thread-SYN-0002-abcdef"},
            "nested_namespace": {**configurable, "checkpoint_ns": "child"},
        }
        for name, value in cases.items():
            with self.subTest(name):
                self.patch_latest(config={"configurable": value})
                self.assert_checkpoint_rejected()
                mock.patch.stopall()

    def test_checkpoint_order_mismatch_rejected(self):
        for order_id in (OTHER_ORDER_ID, "not-an-order", None):
            with self.subTest(order_id=order_id):
                self.patch_latest(values={**self.original, "order_id": order_id})
                self.assert_checkpoint_rejected()
                mock.patch.stopall()

    def test_stale_untrusted_checkpoint_evidence_does_not_block_refresh(self):
        # A checkpoint whose saved VERIFIED claim no longer attests (for
        # example from another process) is discarded, not trusted or reused.
        forged = {**self.original["carrier_evidence"], "verification": DeliveryStatus.VERIFIED}
        self.patch_latest(values={**self.original, "carrier_evidence": forged})
        result = self.service.refresh(THREAD_ID, OWNER)
        self.assertNotEqual(result.state["carrier_evidence"], forged)
        st.validate_state(result.state)

    # Execution failures -----------------------------------------------

    def test_execution_failure_does_not_return_success(self):
        spies = self.spies()
        spies["invoke"].side_effect = RuntimeError(SECRET_TEXT)
        with self.assertRaises(InvestigationRefreshError) as raised:
            self.service.refresh(THREAD_ID, OWNER)
        self.assertNotIn(SECRET_TEXT, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        # Documented non-atomicity: the new thread was registered.
        spies["register"].assert_called_once()

    def test_invalid_or_mismatched_result_rejected(self):
        wrong_order = {**self.original, "order_id": OTHER_ORDER_ID, "oms_evidence": None,
                       "carrier_evidence": None, "oms_analysis": None}
        cases = {
            "none": None,
            "extra_key": {**self.original, "secret": SECRET_TEXT},
            "wrong_order": wrong_order,
        }
        for name, value in cases.items():
            with self.subTest(name):
                with mock.patch.object(self.graph, "invoke", return_value=value):
                    with self.assertRaises(InvestigationRefreshError):
                        self.service.refresh(THREAD_ID, OWNER)

    def test_node_failure_does_not_return_success(self):
        with mock.patch.object(eg, "validate_merged_evidence", side_effect=st.InvalidStateError("x")):
            graph = eg.build_evidence_graph(
                clock=lambda: NOW,
                carrier_adapter=SyntheticCarrierAdapter(clock=lambda: NOW),
                checkpointer=self.saver,
            )
        service = AuthorizedInvestigationRefreshService(self.registry, graph)
        # The same graph serves the checkpoint read and the failing execution.
        with mock.patch.object(graph, "get_state", wraps=graph.get_state) as read, \
                mock.patch.object(graph, "invoke", wraps=graph.invoke) as run, \
                mock.patch.object(self.graph, "get_state", wraps=self.graph.get_state) as other:
            with self.assertRaises(InvestigationRefreshError):
                service.refresh(THREAD_ID, OWNER)
        read.assert_called_once()
        run.assert_called_once()
        other.assert_not_called()

    def test_invalid_generated_thread_id_rejected_before_registration(self):
        spies = self.spies()
        for bad in ("bad id!", "short", THREAD_ID):
            with self.subTest(bad), mock.patch.object(ir.secrets, "token_urlsafe", return_value=bad):
                with self.assertRaises(InvestigationRefreshError):
                    self.service.refresh(THREAD_ID, OWNER)
        self.assert_no_registration_or_execution(spies)

    def test_thread_id_generated_with_secrets(self):
        with mock.patch.object(ir.secrets, "token_urlsafe", wraps=ir.secrets.token_urlsafe) as gen:
            result = self.service.refresh(THREAD_ID, OWNER)
        gen.assert_called_once_with(32)
        self.assertRegex(result.thread_id, THREAD_ID_PATTERN)

    # Interface --------------------------------------------------------

    def test_refresh_accepts_only_thread_id_and_context(self):
        params = list(inspect.signature(self.service.refresh).parameters)
        self.assertEqual(params, ["thread_id", "current_access_context"])
        for extra in ("checkpoint_id", "checkpoint_ns", "tenant_id", "user_id", "created_by",
                      "new_thread_id", "config"):
            with self.subTest(extra), self.assertRaises(TypeError):
                self.service.refresh(THREAD_ID, OWNER, **{extra: "x"})

    def test_no_public_resume_path(self):
        public = {n for n in dir(self.service) if not n.startswith("_")}
        self.assertEqual(public, {"refresh"})
        tree = ast.parse(inspect.getsource(ir))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "invoke"
        ]
        self.assertEqual(len(calls), 1)
        first = calls[0].args[0]
        self.assertIsInstance(first, ast.Call)
        self.assertEqual(getattr(first.func, "id", None), "initial_state")

    def test_wiring_errors(self):
        with self.assertRaises(ValueError):
            AuthorizedInvestigationRefreshService(self.registry, eg.build_evidence_graph())

    def test_checkpoint_reader_cannot_be_injected(self):
        params = list(inspect.signature(AuthorizedInvestigationRefreshService).parameters)
        self.assertEqual(params, ["registry", "graph"])
        # An independently wired reader: different registry, graph and checkpointer.
        other_registry = InMemoryThreadRegistry()
        other_graph = eg.build_evidence_graph(checkpointer=InMemorySaver())
        foreign = AuthorizedCheckpointReader(other_registry, other_graph)
        with self.assertRaises(TypeError):
            AuthorizedInvestigationRefreshService(self.registry, foreign, self.graph)
        with self.assertRaises(TypeError):
            AuthorizedInvestigationRefreshService(
                self.registry, self.graph, checkpoint_reader=foreign
            )
        # The internal reader is bound to the same trusted dependencies.
        self.assertIsNot(self.service._reader, foreign)
        self.assertIsInstance(self.service._reader, AuthorizedCheckpointReader)
        self.assertIs(self.service._reader._registry, self.registry)
        self.assertIs(self.service._reader._graph, self.graph)

    def test_repr_omits_internals(self):
        self.assertEqual(repr(self.service), "AuthorizedInvestigationRefreshService()")
        result = self.service.refresh(THREAD_ID, OWNER)
        self.assertNotIn(ORDER_ID, repr(result))


class ImportIsolationTests(unittest.TestCase):
    def test_refresh_does_not_import_langgraph(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        code = (
            "import sys\n"
            "import northstar.state.investigation_refresh\n"
            "loaded = [m for m in sys.modules if m.split('.')[0] == 'langgraph']\n"
            "sys.exit(1 if loaded else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": src},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
