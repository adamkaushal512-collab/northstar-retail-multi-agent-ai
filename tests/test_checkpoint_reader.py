"""Unit tests for authorized, read-only checkpoint access (Step 3.6F.21).

Uses the real evidence graph with a real LangGraph ``InMemorySaver`` plus
``mock`` spies on the graph and saver read methods. Synthetic data only. No
AWS, Bedrock, carrier or network calls.
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

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
from northstar.state.thread_registry import THREAD_CHECK, InMemoryThreadRegistry

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
if HAS_LANGGRAPH:
    from langgraph.checkpoint.memory import InMemorySaver

    from northstar.orchestration.multi_agent import evidence_graph as eg
    from northstar.orchestration.multi_agent.state import InvestigationRuntimeContext, initial_state

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
THREAD_ID = "thread-SYN-0001-abcdef"
OTHER_THREAD_ID = "thread-SYN-0002-abcdef"
UNKNOWN_THREAD_ID = "thread-unknown-000000"
ORDER_ID = "SYN-ORD-0004"


def context(user_id="SYN-USER-0001", tenant_id="SYN-TENANT-A", perms=None, scopes=None):
    return AccessContext(
        user_id,
        tenant_id,
        frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}) if perms is None else perms,
        frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}) if scopes is None else scopes,
    )


OWNER = context()
OTHER_USER = context(user_id="SYN-USER-0002")
OTHER_TENANT = context(tenant_id="SYN-TENANT-B")
REVOKED_PERMISSION = context(perms=frozenset({CARRIER_TRACKING_READ}))
REVOKED_SCOPE = context(scopes=frozenset({CARRIER_READ_SCOPE}))

DENIED_CASES = (
    ("wrong_tenant", THREAD_ID, OTHER_TENANT),
    ("wrong_user", THREAD_ID, OTHER_USER),
    ("revoked_permission", THREAD_ID, REVOKED_PERMISSION),
    ("revoked_scope", THREAD_ID, REVOKED_SCOPE),
    ("no_context", THREAD_ID, None),
    ("unknown_thread", UNKNOWN_THREAD_ID, OWNER),
    ("invalid_thread_id", "bad id", OWNER),
)


def config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


@unittest.skipUnless(HAS_LANGGRAPH, "optional langgraph extra not installed")
class CheckpointReaderTests(unittest.TestCase):
    def setUp(self):
        self.saver = InMemorySaver()
        self.graph = eg.build_evidence_graph(
            clock=lambda: NOW,
            carrier_adapter=SyntheticCarrierAdapter(clock=lambda: NOW),
            checkpointer=self.saver,
        )
        self.registry = InMemoryThreadRegistry()
        self.registry.register_thread(THREAD_ID, ORDER_ID, OWNER)
        self.final = self.graph.invoke(
            initial_state(ORDER_ID),
            context=InvestigationRuntimeContext(access_context=OWNER),
            config=config(THREAD_ID),
        )
        self.reader = AuthorizedCheckpointReader(self.registry, self.graph)

    def spies(self):
        """Patch graph and saver read methods with call-through spies."""
        patches = [
            mock.patch.object(self.graph, "get_state", wraps=self.graph.get_state),
            mock.patch.object(self.graph, "get_state_history", wraps=self.graph.get_state_history),
            mock.patch.object(self.saver, "get_tuple", wraps=self.saver.get_tuple),
            mock.patch.object(self.saver, "list", wraps=self.saver.list),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        return mocks

    # Authorized reads -------------------------------------------------

    def test_owner_reads_latest_checkpoint(self):
        get_state, get_history, get_tuple, saver_list = self.spies()
        snapshot = self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
        self.assertEqual(snapshot.values, self.final)
        self.assertEqual(snapshot.next, ())
        get_state.assert_called_once_with(config(THREAD_ID))
        self.assertTrue(get_tuple.called)
        get_history.assert_not_called()
        saver_list.assert_not_called()

    def test_owner_reads_history_newest_first(self):
        get_state, get_history, _, saver_list = self.spies()
        history = self.reader.get_checkpoint_history(THREAD_ID, OWNER)
        self.assertIsInstance(history, tuple)
        self.assertEqual([h.metadata["step"] for h in history], [3, 2, 1, 0, -1])
        self.assertEqual(history[0].values, self.final)
        get_history.assert_called_once_with(config(THREAD_ID))
        self.assertTrue(saver_list.called)
        get_state.assert_not_called()

    def test_owner_with_fresh_equivalent_context(self):
        snapshot = self.reader.get_latest_checkpoint(THREAD_ID, context())
        self.assertEqual(snapshot.values, self.final)

    def test_registered_thread_without_checkpoints(self):
        self.registry.register_thread(OTHER_THREAD_ID, ORDER_ID, OWNER)
        self.assertIsNone(self.reader.get_latest_checkpoint(OTHER_THREAD_ID, OWNER))
        self.assertEqual(self.reader.get_checkpoint_history(OTHER_THREAD_ID, OWNER), ())

    def test_reads_are_isolated_to_the_authorized_thread(self):
        self.registry.register_thread(OTHER_THREAD_ID, ORDER_ID, OWNER)
        self.graph.invoke(initial_state(ORDER_ID), context=None, config=config(OTHER_THREAD_ID))
        for snapshot in self.reader.get_checkpoint_history(THREAD_ID, OWNER):
            self.assertEqual(snapshot.config["configurable"]["thread_id"], THREAD_ID)
        other = self.reader.get_latest_checkpoint(OTHER_THREAD_ID, OWNER)
        self.assertNotEqual(other.values, self.final)

    def test_reads_do_not_write_checkpoints(self):
        before = list(self.saver.list(None))
        self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
        self.reader.get_checkpoint_history(THREAD_ID, OWNER)
        self.assertEqual(list(self.saver.list(None)), before)

    # Denials ----------------------------------------------------------

    def test_denied_requests_perform_zero_checkpoint_reads(self):
        mocks = self.spies()
        for name, thread_id, ctx in DENIED_CASES:
            for read in (self.reader.get_latest_checkpoint, self.reader.get_checkpoint_history):
                with self.subTest(case=name, read=read.__name__):
                    with self.assertRaises(AccessDeniedError) as caught:
                        read(thread_id, ctx)
                    self.assertEqual(str(caught.exception), DENIED_MESSAGE)
                    for spy in mocks:
                        self.assertEqual(spy.mock_calls, [])

    def test_thread_denials_are_indistinguishable(self):
        checks = set()
        for name in ("wrong_tenant", "wrong_user", "unknown_thread", "invalid_thread_id"):
            thread_id, ctx = next((t, c) for n, t, c in DENIED_CASES if n == name)
            with self.assertRaises(AccessDeniedError) as caught:
                self.reader.get_checkpoint_history(thread_id, ctx)
            checks.add((caught.exception.check, str(caught.exception)))
        self.assertEqual(checks, {(THREAD_CHECK, DENIED_MESSAGE)})

    def test_denial_exposes_no_checkpoint_or_identity_data(self):
        sensitive = (THREAD_ID, ORDER_ID, OWNER.user_id, OWNER.tenant_id, json.dumps(self.final, default=str))
        for name, thread_id, ctx in DENIED_CASES:
            with self.subTest(case=name), self.assertRaises(AccessDeniedError) as caught:
                self.reader.get_latest_checkpoint(thread_id, ctx)
            exc = caught.exception
            rendered = " ".join((str(exc), repr(exc), repr(exc.args), str(exc.check)))
            for value in sensitive:
                self.assertNotIn(value, rendered)

    def test_revocation_takes_effect_on_next_read(self):
        self.assertIsNotNone(self.reader.get_latest_checkpoint(THREAD_ID, OWNER))
        with self.assertRaises(AccessDeniedError) as caught:
            self.reader.get_latest_checkpoint(THREAD_ID, REVOKED_PERMISSION)
        self.assertEqual(caught.exception.check, "user")

    def test_authorization_precedes_each_checkpoint_call(self):
        order = []
        authorize = self.registry.authorize_thread_access

        def spy_authorize(*args):
            order.append("authorize")
            return authorize(*args)

        def spy(name, fn):
            def wrapper(*args, **kwargs):
                order.append(name)
                return fn(*args, **kwargs)
            return wrapper

        with mock.patch.object(self.registry, "authorize_thread_access", side_effect=spy_authorize), \
                mock.patch.object(self.graph, "get_state", side_effect=spy("get_state", self.graph.get_state)), \
                mock.patch.object(
                    self.graph, "get_state_history", side_effect=spy("history", self.graph.get_state_history)
                ):
            self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
            self.reader.get_checkpoint_history(THREAD_ID, OWNER)
        self.assertEqual(order, ["authorize", "get_state", "authorize", "history"])

    # Input surface ----------------------------------------------------

    def test_caller_cannot_supply_identity_or_config_overrides(self):
        overrides = (
            {"tenant_id": OWNER.tenant_id},
            {"user_id": OWNER.user_id},
            {"created_by": OWNER.user_id},
            {"checkpoint_id": "x"},
            {"checkpoint_ns": "x"},
            {"config": config(THREAD_ID)},
        )
        mocks = self.spies()
        for kwargs in overrides:
            for read in (self.reader.get_latest_checkpoint, self.reader.get_checkpoint_history):
                with self.subTest(kwargs=kwargs, read=read.__name__), self.assertRaises(TypeError):
                    read(THREAD_ID, OWNER, **kwargs)
        for spy in mocks:
            self.assertEqual(spy.mock_calls, [])

    def test_graph_without_checkpointer_rejected(self):
        with self.assertRaises(ValueError):
            AuthorizedCheckpointReader(self.registry, eg.build_evidence_graph())

    def test_snapshot_for_another_thread_fails_closed(self):
        foreign = self.graph.get_state(config(THREAD_ID))
        foreign = foreign._replace(config={"configurable": {**foreign.config["configurable"], "thread_id": "x"}})
        with mock.patch.object(self.graph, "get_state", return_value=foreign):
            with self.assertRaises(CheckpointIntegrityError):
                self.reader.get_latest_checkpoint(THREAD_ID, OWNER)

    def test_snapshot_from_another_namespace_fails_closed(self):
        snap = self.graph.get_state(config(THREAD_ID))
        nested = snap._replace(config={"configurable": {**snap.config["configurable"], "checkpoint_ns": "sub"}})
        with mock.patch.object(self.graph, "get_state_history", return_value=iter([nested])):
            with self.assertRaises(CheckpointIntegrityError):
                self.reader.get_checkpoint_history(THREAD_ID, OWNER)

    # Empty vs. malformed snapshots (Step 3.6F.25) -----------------------

    def empty_snapshot(self, thread_id):
        """The real snapshot LangGraph returns for a thread with no checkpoints."""
        return self.graph.get_state(config(thread_id))

    def assert_latest_fails_closed(self, snapshot):
        with mock.patch.object(self.graph, "get_state", return_value=snapshot):
            with self.assertRaises(CheckpointIntegrityError) as caught:
                self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
        self.assertNotIn(THREAD_ID, str(caught.exception))

    def test_legitimate_empty_snapshot_returns_none(self):
        self.registry.register_thread(OTHER_THREAD_ID, ORDER_ID, OWNER)
        empty = self.empty_snapshot(OTHER_THREAD_ID)
        self.assertEqual(empty.config, config(OTHER_THREAD_ID))
        with mock.patch.object(self.graph, "get_state", return_value=empty) as get_state:
            self.assertIsNone(self.reader.get_latest_checkpoint(OTHER_THREAD_ID, OWNER))
        get_state.assert_called_once_with(config(OTHER_THREAD_ID))

    def test_malformed_snapshots_fail_closed(self):
        empty = self.empty_snapshot(THREAD_ID + "-x")._replace(config=config(THREAD_ID))
        cases = {
            "none": None,
            "no_config": object(),
            "config_not_dict": empty._replace(config="bad"),
            "configurable_missing": empty._replace(config={}),
            "configurable_not_dict": empty._replace(config={"configurable": ["x"]}),
            "extra_config_key": empty._replace(
                config={"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": ""}}
            ),
            "values_without_checkpoint_id": empty._replace(values={"order_id": ORDER_ID}),
            "next_without_checkpoint_id": empty._replace(next=("oms",)),
            "metadata_without_checkpoint_id": empty._replace(metadata={"step": 0}),
        }
        for name, snapshot in cases.items():
            with self.subTest(case=name):
                self.assert_latest_fails_closed(snapshot)

    def test_foreign_thread_without_checkpoint_id_fails_closed(self):
        self.assert_latest_fails_closed(self.empty_snapshot(OTHER_THREAD_ID))

    def test_foreign_namespace_without_checkpoint_id_fails_closed(self):
        nested = self.empty_snapshot(THREAD_ID + "-x")._replace(
            config={"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": "sub"}}
        )
        self.assert_latest_fails_closed(nested)

    def test_empty_snapshot_checks_follow_authorization(self):
        self.registry.register_thread(OTHER_THREAD_ID, ORDER_ID, OWNER)
        get_state = mock.Mock(return_value=None)
        with mock.patch.object(self.graph, "get_state", get_state):
            with self.assertRaises(AccessDeniedError) as caught:
                self.reader.get_latest_checkpoint(OTHER_THREAD_ID, OTHER_USER)
        self.assertEqual(str(caught.exception), DENIED_MESSAGE)
        get_state.assert_not_called()

    # Checkpoint ID integrity (Step 3.6F.27) ----------------------------

    INVALID_CHECKPOINT_IDS = {
        "none": None,
        "empty_string": "",
        "int": 123,
        "zero": 0,
        "bytes": b"checkpoint",
        "list": ["checkpoint"],
        "dict": {"id": "checkpoint"},
        "bool": True,
    }

    def with_checkpoint_id(self, snapshot, checkpoint_id=mock.sentinel.missing):
        configurable = dict(snapshot.config["configurable"])
        configurable.pop("checkpoint_id", None)
        if checkpoint_id is not mock.sentinel.missing:
            configurable["checkpoint_id"] = checkpoint_id
        return snapshot._replace(config={"configurable": configurable})

    def invalid_id_cases(self, snapshot):
        cases = {"missing": self.with_checkpoint_id(snapshot)}
        for name, value in self.INVALID_CHECKPOINT_IDS.items():
            cases[name] = self.with_checkpoint_id(snapshot, value)
        return cases

    def test_valid_checkpoint_ids_are_nonempty_strings(self):
        latest = self.reader.get_latest_checkpoint(THREAD_ID, OWNER)
        latest_id = latest.config["configurable"]["checkpoint_id"]
        self.assertIsInstance(latest_id, str)
        self.assertNotEqual(latest_id, "")
        history = self.reader.get_checkpoint_history(THREAD_ID, OWNER)
        self.assertTrue(history)
        for snapshot in history:
            checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
            self.assertIsInstance(checkpoint_id, str)
            self.assertNotEqual(checkpoint_id, "")
        self.assertEqual(history[0].config["configurable"]["checkpoint_id"], latest_id)

    def test_latest_with_invalid_checkpoint_id_fails_closed(self):
        snapshot = self.graph.get_state(config(THREAD_ID))
        for name, bad in self.invalid_id_cases(snapshot).items():
            with self.subTest(case=name):
                self.assert_latest_fails_closed(bad)

    def test_empty_shaped_latest_with_invalid_checkpoint_id_fails_closed(self):
        empty = self.empty_snapshot(THREAD_ID + "-x")._replace(config=config(THREAD_ID))
        for name, value in self.INVALID_CHECKPOINT_IDS.items():
            with self.subTest(case=name):
                self.assert_latest_fails_closed(self.with_checkpoint_id(empty, value))

    def test_history_entry_with_invalid_checkpoint_id_fails_closed(self):
        history = list(self.graph.get_state_history(config(THREAD_ID)))
        for name, bad in self.invalid_id_cases(history[-1]).items():
            for position in ("first", "last"):
                entries = [bad, *history[1:]] if position == "first" else [*history[:-1], bad]
                with self.subTest(case=name, position=position):
                    with mock.patch.object(self.graph, "get_state_history", return_value=iter(entries)):
                        with self.assertRaises(CheckpointIntegrityError) as caught:
                            self.reader.get_checkpoint_history(THREAD_ID, OWNER)
                    self.assertNotIn(THREAD_ID, str(caught.exception))

    def test_history_rejects_empty_shaped_entry(self):
        empty = self.empty_snapshot(THREAD_ID + "-x")._replace(config=config(THREAD_ID))
        with mock.patch.object(self.graph, "get_state_history", return_value=iter([empty])):
            with self.assertRaises(CheckpointIntegrityError):
                self.reader.get_checkpoint_history(THREAD_ID, OWNER)

    def test_checkpoint_id_checks_follow_authorization(self):
        bad = self.with_checkpoint_id(self.graph.get_state(config(THREAD_ID)), "")
        get_state = mock.Mock(return_value=bad)
        get_history = mock.Mock(return_value=iter([bad]))
        with mock.patch.object(self.graph, "get_state", get_state), \
                mock.patch.object(self.graph, "get_state_history", get_history):
            for read in (self.reader.get_latest_checkpoint, self.reader.get_checkpoint_history):
                with self.subTest(read=read.__name__), self.assertRaises(AccessDeniedError):
                    read(THREAD_ID, OTHER_USER)
        get_state.assert_not_called()
        get_history.assert_not_called()

    def test_repr_omits_internals(self):
        self.assertEqual(repr(self.reader), "AuthorizedCheckpointReader()")


class ImportIsolationTests(unittest.TestCase):
    def test_reader_does_not_import_langgraph(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        code = (
            "import sys\n"
            "import northstar.state.checkpoint_reader\n"
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


if __name__ == "__main__":
    unittest.main()
