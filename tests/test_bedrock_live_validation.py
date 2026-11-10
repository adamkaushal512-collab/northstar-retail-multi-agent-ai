"""Offline tests for the opt-in live Bedrock validation logic (Step 2.6G).

Mocked Converse client only. Synthetic data. No AWS, Bedrock or network
calls, no credentials. The live script itself is never executed here
beyond its opt-in refusal.
"""

import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest import mock

from northstar.evidence.investigation_schema import DelayedDeliveryInvestigation
from northstar.llm import bedrock_live_validation as live
from northstar.llm.bedrock_converse import INVESTIGATION_TOOL_NAME

ORDER = live.SYNTHETIC_ORDER_ID
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "examples" / "bedrock_live_validation.py"


def response(content, stop_reason, tokens=(100, 20)):
    return {
        "ResponseMetadata": {"RequestId": "test-request-id"},
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1], "totalTokens": sum(tokens)},
    }


def tool_use(name, tool_use_id, tool_input=None):
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input or {"order_id": ORDER}}}


FINAL = response([{"text": "Summary: MODEL-FREE-TEXT-MARKER"}], "end_turn")


def structured(**overrides):
    draft = {
        "order_id": ORDER,
        "delivery_status": "UNVERIFIED",
        "summary": "Draft.",
        "likely_cause": None,
        "evidence": [],
        "missing_information": [],
        "recommended_next_steps": [],
        "requires_human_review": True,
    }
    draft.update(overrides)
    return response([tool_use(INVESTIGATION_TOOL_NAME, "rec-1", draft)], "tool_use")


def client_with(*responses):
    client = mock.Mock()
    client.converse.side_effect = list(responses)
    return client


def run(client, **kwargs):
    return live.run_validation(client, model_id="m", region="r", policy=live.live_retry_policy(), **kwargs)


def statuses(report):
    return {c.name: c.status for c in report.checks}


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.client = client_with(
            response([tool_use("get_order_details", "tu-1"), tool_use("get_carrier_tracking", "tu-2")], "tool_use"),
            FINAL,
            structured(),
        )
        self.report = run(self.client)

    def test_all_checks_pass_and_probe_is_skipped(self):
        s = statuses(self.report)
        self.assertEqual(self.report.verdict, live.PASS, self.report.render())
        self.assertEqual(self.report.exit_code(), 0)
        self.assertFalse(any(name.startswith("probe.") for name in s))
        for name in (
            "preflight.missing_context_fails_closed",
            "preflight.carrier_permission_denied",
            "loop.completed",
            "loop.allowlisted_tools_only",
            "loop.tool_use_id_pairing",
            "loop.get_order_details",
            "loop.get_carrier_tracking",
            "loop.carrier_verification_from_platform",
            "loop.audit_events",
            "structured.schema_validated",
            "structured.requires_human_review",
            "structured.order_id_matches",
            "structured.delivery_status_supported",
        ):
            self.assertEqual(s.get(name), live.PASS, name)

    def test_counts_requests_and_tokens(self):
        # Preflight sends nothing: three calls are loop (2) + structured (1).
        self.assertEqual(self.report.request_count, 3)
        self.assertEqual(self.client.converse.call_count, 3)
        self.assertEqual(self.report.usage, {"inputTokens": 300, "outputTokens": 60, "totalTokens": 360})
        self.assertEqual(self.report.tools_requested, ("get_order_details", "get_carrier_tracking"))

    def test_explicit_max_tokens_on_every_request(self):
        max_tokens = [c.kwargs["inferenceConfig"]["maxTokens"] for c in self.client.converse.call_args_list]
        self.assertEqual(max_tokens, [live.LOOP_MAX_TOKENS, live.LOOP_MAX_TOKENS, live.STRUCTURED_MAX_TOKENS])

    def test_structured_call_uses_platform_verification(self):
        question = self.client.converse.call_args_list[2].kwargs["messages"][0]["content"][0]["text"]
        # SYN-ORD-0001 has fresh synthetic carrier data, so the platform verified it.
        self.assertIn("Current carrier status verified by the platform: yes", question)

    def test_rendered_output_is_sanitized(self):
        text = self.report.render()
        for secret in (ORDER, "SYN-TENANT-A", live.SYNTHETIC_CONTEXT.user_id, "SYN-TRK", "SYN-CUST",
                       "MODEL-FREE-TEXT-MARKER", "test-request-id", "tu-1", '"order_id"', "WEATHER"):
            self.assertNotIn(secret, text)
        self.assertIn("Verdict: PASS", text)
        self.assertIn("totalTokens=360", text)


class ModelChoiceTests(unittest.TestCase):
    def test_unrequested_tool_is_not_exercised_and_probe_runs(self):
        client = client_with(
            response([tool_use("get_order_details", "tu-1")], "tool_use"),
            FINAL,
            response([tool_use("get_carrier_tracking", "probe-1")], "tool_use"),
            structured(),
        )
        report = run(client)
        s = statuses(report)
        self.assertEqual(s["loop.get_order_details"], live.PASS)
        self.assertEqual(s["loop.get_carrier_tracking"], live.NOT_EXERCISED)
        self.assertNotIn("loop.carrier_verification_from_platform", s)
        self.assertEqual(s["probe.tool_choice.get_carrier_tracking"], live.PASS)
        self.assertNotIn("probe.tool_choice.get_order_details", s)
        # A probe pass never upgrades loop coverage.
        self.assertEqual(report.verdict, "PARTIAL")
        self.assertEqual(report.exit_code(), 2)
        probe_request = client.converse.call_args_list[2].kwargs
        self.assertEqual(probe_request["toolConfig"]["toolChoice"], {"tool": {"name": "get_carrier_tracking"}})
        self.assertEqual(probe_request["inferenceConfig"]["maxTokens"], live.PROBE_MAX_TOKENS)

    def test_no_probe_flag_skips_probe(self):
        client = client_with(response([tool_use("get_order_details", "tu-1")], "tool_use"), FINAL, structured())
        report = run(client, probe=False)
        self.assertFalse(any(c.name.startswith("probe.") for c in report.checks))
        self.assertEqual(report.request_count, 3)

    def test_unsupported_tool_fails_closed(self):
        client = client_with(response([tool_use("issue_refund", "tu-1")], "tool_use"), structured())
        report = run(client, probe=False)
        s = statuses(report)
        self.assertEqual(s["loop.completed"], live.FAIL)
        self.assertEqual(s["loop.allowlisted_tools_only"], live.FAIL)
        self.assertIn("UnsupportedTool", next(c.detail for c in report.checks if c.name == "loop.completed"))
        self.assertNotIn("issue_refund", report.render())
        self.assertEqual(report.verdict, live.FAIL)


class StructuredTests(unittest.TestCase):
    def _run(self, structured_response):
        client = client_with(
            response([tool_use("get_order_details", "tu-1"), tool_use("get_carrier_tracking", "tu-2")], "tool_use"),
            FINAL,
            structured_response,
        )
        return run(client)

    def test_verified_allowed_when_platform_verified(self):
        report = self._run(structured(delivery_status="VERIFIED"))
        self.assertEqual(statuses(report)["structured.delivery_status_supported"], live.PASS)

    def test_requires_human_review_false_is_rejected(self):
        report = self._run(structured(requires_human_review=False))
        check = next(c for c in report.checks if c.name == "structured.schema_validated")
        self.assertEqual(check.status, live.FAIL)
        self.assertIn("SchemaValidationFailed", check.detail)

    def test_malformed_output_is_rejected(self):
        report = self._run(response([{"text": "not a tool call"}], "end_turn"))
        check = next(c for c in report.checks if c.name == "structured.schema_validated")
        self.assertEqual(check.status, live.FAIL)
        self.assertIn("MissingToolUse", check.detail)

    def test_verified_without_platform_verification_fails(self):
        report = live.ValidationReport()
        investigation = DelayedDeliveryInvestigation.model_validate_json(
            '{"order_id": "SYN-ORD-0001", "delivery_status": "VERIFIED", "summary": "", "likely_cause": null,'
            ' "evidence": [], "missing_information": [], "recommended_next_steps": [], "requires_human_review": true}'
        )
        live.evaluate_structured(report, investigation, ORDER, verified=False)
        self.assertEqual(statuses(report)["structured.delivery_status_supported"], live.FAIL)


class BudgetTests(unittest.TestCase):
    def test_request_cap_stops_further_calls(self):
        client = client_with(response([tool_use("get_order_details", "tu-1")], "tool_use"), FINAL, structured())
        report = run(client, max_requests=1)
        self.assertEqual(client.converse.call_count, 1)
        self.assertEqual(report.request_count, 1)
        s = statuses(report)
        self.assertEqual(s["loop.completed"], live.FAIL)
        self.assertEqual(s["structured.schema_validated"], live.FAIL)
        self.assertIn("LiveCallBudgetExceeded", report.render())

    def test_cap_must_be_positive(self):
        with self.assertRaises(ValueError):
            live.MeteredClient(mock.Mock(), 0)

    def test_scenario_not_retried_on_non_transient_error(self):
        client = mock.Mock()
        client.converse.side_effect = RuntimeError("unexpected")
        with self.assertRaises(RuntimeError):
            run(client)
        self.assertEqual(client.converse.call_count, 1)


class PairingTests(unittest.TestCase):
    def _exchanges(self, result_ids, final_uses=()):
        first_req = {"messages": [{"role": "user", "content": [{"text": "q"}]}]}
        first_resp = response([tool_use("get_order_details", "a"), tool_use("get_carrier_tracking", "b")], "tool_use")
        second_req = {"messages": [
            {"role": "user", "content": [{"text": "q"}]},
            first_resp["output"]["message"],
            {"role": "user", "content": [{"toolResult": {"toolUseId": i, "status": "success", "content": []}}
                                         for i in result_ids]},
        ]}
        second_resp = response([tool_use("get_order_details", u) for u in final_uses] or [{"text": "x"}],
                               "tool_use" if final_uses else "end_turn")
        return [(first_req, first_resp), (second_req, second_resp)]

    def test_matching_ids_pass(self):
        ok, detail = live.check_tool_result_pairing(self._exchanges(["a", "b"]))
        self.assertTrue(ok)
        self.assertIn("2 toolUse/toolResult", detail)

    def test_reordered_or_missing_ids_fail(self):
        self.assertFalse(live.check_tool_result_pairing(self._exchanges(["b", "a"]))[0])
        self.assertFalse(live.check_tool_result_pairing(self._exchanges(["a"]))[0])

    def test_unanswered_final_tool_use_fails(self):
        self.assertFalse(live.check_tool_result_pairing(self._exchanges(["a", "b"], final_uses=["c"]))[0])


class ProbeEvaluationTests(unittest.TestCase):
    def test_wrong_tool_or_invalid_input_fails(self):
        wrong = response([tool_use("get_order_details", "p")], "tool_use")
        self.assertEqual(live.evaluate_probe_response(wrong, "get_carrier_tracking")[0], live.FAIL)
        bad = response([tool_use("get_carrier_tracking", "p", {"order_id": ORDER, "tenant_id": "x"})], "tool_use")
        self.assertEqual(live.evaluate_probe_response(bad, "get_carrier_tracking")[0], live.FAIL)
        text_only = response([{"text": "no"}], "end_turn")
        self.assertEqual(live.evaluate_probe_response(text_only, "get_carrier_tracking")[0], live.FAIL)

    def test_valid_forced_call_passes_without_execution(self):
        ok = response([tool_use("get_carrier_tracking", "p")], "tool_use")
        with mock.patch("northstar.tools.registry.run_named_tool") as runner:
            self.assertEqual(live.evaluate_probe_response(ok, "get_carrier_tracking")[0], live.PASS)
        runner.assert_not_called()


class OptInTests(unittest.TestCase):
    def test_requires_both_env_and_flag(self):
        self.assertTrue(live.opted_in({live.OPT_IN_ENV: "1"}, [live.CONFIRM_FLAG]))
        self.assertFalse(live.opted_in({}, [live.CONFIRM_FLAG]))
        self.assertFalse(live.opted_in({live.OPT_IN_ENV: "1"}, []))
        self.assertFalse(live.opted_in({live.OPT_IN_ENV: "true"}, [live.CONFIRM_FLAG]))

    def test_script_refuses_without_opt_in_and_creates_no_client(self):
        spec = importlib.util.spec_from_file_location("bedrock_live_validation_script", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(script, "create_client_with_timeouts") as create, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(script.main([live.CONFIRM_FLAG]), 3)
        create.assert_not_called()
        self.assertIn("Not run", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
