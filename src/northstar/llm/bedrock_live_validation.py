"""Opt-in live Bedrock validation checks (Step 2.6G).

[Proposal] Experiment only; ADR-001 and ADR-005 remain Proposed. Nothing here
selects an architecture or an agent count, and nothing links the tool loop
to the structured-output path: they are validated as two separate calls.

This module holds the check logic so it can be tested offline with a mocked
client. It never creates a Bedrock client and is never run by unit-test
discovery or CI. The only live entry point is the manual script
``examples/bedrock_live_validation.py``, which requires an explicit opt-in.

What is exercised:
1. Local preflight (no model call): a missing access context fails closed
   before any Converse request, and a context without carrier permission is
   denied by the carrier tool.
2. The real tool loop (``run_tool_loop``) with the synthetic OMS and carrier
   adapters, an authorized synthetic ``AccessContext`` and a synthetic order
   in that tenant. Checks: only allowlisted tools were requested and run,
   every ``toolUseId`` is answered by exactly one ``toolResult`` in the next
   request, in order, and which tools the model actually chose. A tool the
   model did not request is reported NOT_EXERCISED, never PASS.
3. Optional tool-choice probe (separate, minimal Converse call), only for
   tools the loop did not exercise. It checks that a forced ``toolChoice``
   returns a schema-valid ``toolUse`` for that tool. The tool is NOT run and
   the probe does not count as tool-loop coverage.
4. The existing structured path (``investigate_delay_structured``) with
   synthetic evidence and a ``carrier_status_verified`` flag computed by
   platform code from the synthetic carrier tool, never from model text.

Read-only throughout [Decision D11, D18]. No refunds, reships, order
changes, real OMS or carrier calls, MCP or LangGraph. Output is sanitized:
check names, statuses, error codes, counts and token totals only. Never
prompts, tool arguments or results, model text, order, customer or tenant
identifiers, request IDs or exception messages (an AWS error message can
contain an account ID).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from northstar.evidence.investigation_schema import DeliveryStatus
from northstar.llm.bedrock_converse import (
    BedrockInvocationError,
    InvalidStructuredOutputError,
    investigate_delay_structured,
    resolve_config,
)
from northstar.llm.bedrock_retry import RetryPolicy, converse_with_retry
from northstar.llm.bedrock_tool_loop import ToolLoopError, run_tool_loop
from northstar.observability.audit import InMemoryAuditSink
from northstar.security.access_context import (
    CARRIER_READ_SCOPE,
    CARRIER_TRACKING_READ,
    OMS_ORDER_READ,
    OMS_READ_SCOPE,
    AccessContext,
    AccessDeniedError,
)
from northstar.tools import carrier_tracking, order_details, registry

# Opt-in guard: both the environment variable and the CLI flag are required.
OPT_IN_ENV = "NORTHSTAR_RUN_LIVE_BEDROCK"
OPT_IN_VALUE = "1"
CONFIRM_FLAG = "--confirm-paid-calls"

# [SYNTHETIC] Authorized user in tenant A; SYN-ORD-0001 belongs to tenant A and
# has fresh synthetic carrier tracking (weather exception scenario).
SYNTHETIC_ORDER_ID = "SYN-ORD-0001"
SYNTHETIC_CONTEXT = AccessContext(
    user_id="SYN-USER-LIVE-0001",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ, CARRIER_TRACKING_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE, CARRIER_READ_SCOPE}),
)
_OMS_ONLY_CONTEXT = AccessContext(
    user_id="SYN-USER-LIVE-0002",
    tenant_id="SYN-TENANT-A",
    user_permissions=frozenset({OMS_ORDER_READ}),
    service_scopes=frozenset({OMS_READ_SCOPE}),
)

LOOP_QUESTION = (
    f"A customer service agent asks why order {SYNTHETIC_ORDER_ID} is delayed. "
    "Use the read-only tools to retrieve the order details and the carrier "
    "tracking for this order, then draft the investigation."
)
_PROBE_SYSTEM_PROMPT = (
    "You are testing tool selection for a read-only lookup. Call the requested "
    "tool once with only the arguments its schema allows."
)

# [Assumption] Experiment bounds for one live run, not NFR targets.
LOOP_MAX_TOKENS = 800
LOOP_MAX_TOOL_ROUNDS = 3  # at most 4 Converse calls for the loop
STRUCTURED_MAX_TOKENS = 1200
PROBE_MAX_TOKENS = 200
# Hard cap on Converse attempts across the whole run, including retries.
DEFAULT_MAX_REQUESTS = 10


def live_retry_policy() -> RetryPolicy:
    """Existing bounded retry, with a longer read timeout for full model replies.

    [Assumption] The 5 s NFR proposal per attempt is too short for a complete
    draft; 30 s is used for this experiment only. Two attempts at most, and
    only for transient failures (``bedrock_retry.is_transient``).
    """
    return RetryPolicy(max_attempts=2, read_timeout_seconds=30.0, total_budget_seconds=60.0)


PASS = "PASS"
FAIL = "FAIL"
NOT_EXERCISED = "NOT_EXERCISED"
SKIPPED = "SKIPPED"


class LiveCallBudgetExceeded(BedrockInvocationError):
    """The run reached its Converse request cap; nothing further is sent."""


class MeteredClient:
    """Wraps a bedrock-runtime client: counts, caps and records Converse calls.

    Requests and responses are kept in memory for pairing checks only and are
    never printed. Every attempt counts, including failed ones.
    """

    def __init__(self, client: Any, max_requests: int = DEFAULT_MAX_REQUESTS) -> None:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        self._client = client
        self.max_requests = max_requests
        self.request_count = 0
        self.exchanges: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.usage: dict[str, int] = {}

    def converse(self, **request: Any) -> dict[str, Any]:
        if self.request_count >= self.max_requests:
            raise LiveCallBudgetExceeded(
                f"live request cap reached ({self.max_requests})", code="LiveCallBudgetExceeded"
            )
        self.request_count += 1
        self.exchanges.append((copy.deepcopy(request), None))
        response = self._client.converse(**request)
        self.exchanges[-1] = (self.exchanges[-1][0], copy.deepcopy(response))
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            for key in ("inputTokens", "outputTokens", "totalTokens"):
                if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool):
                    self.usage[key] = self.usage.get(key, 0) + usage[key]
        return response

    def mark(self) -> int:
        """Index of the next exchange, to slice one phase's calls."""
        return len(self.exchanges)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""  # sanitized: codes, counts and fixed wording only


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)
    model_id: str = ""
    region: str = ""
    request_count: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    tools_requested: tuple[str, ...] = ()

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def verdict(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return FAIL
        if NOT_EXERCISED in statuses:
            return "PARTIAL"
        return PASS

    def exit_code(self) -> int:
        return {PASS: 0, "PARTIAL": 2}.get(self.verdict, 1)

    def render(self) -> str:
        lines = [f"Live Bedrock validation (model_id={self.model_id}, region={self.region})"]
        width = max((len(c.name) for c in self.checks), default=0)
        for c in self.checks:
            suffix = f"  {c.detail}" if c.detail else ""
            lines.append(f"  {c.name.ljust(width)}  {c.status}{suffix}")
        lines.append(f"Tools requested by the model in the tool loop: {', '.join(self.tools_requested) or 'none'}")
        lines.append(f"Bedrock Converse requests (attempts, incl. retries): {self.request_count}")
        tokens = ", ".join(f"{k}={self.usage[k]}" for k in ("inputTokens", "outputTokens", "totalTokens") if k in self.usage)
        lines.append(f"Token usage: {tokens or 'not reported'}")
        lines.append(f"Verdict: {self.verdict}")
        return "\n".join(lines)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.isidentifier() else type(exc).__name__


# --- Pure checks (offline-tested) -----------------------------------------


def _blocks(message: object) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def requested_tool_uses(exchanges: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> list[list[dict[str, Any]]]:
    """toolUse blocks of each response, in call order (empty list if none)."""
    result = []
    for _, response in exchanges:
        message = (response or {}).get("output", {}).get("message") if isinstance(response, dict) else None
        result.append([b["toolUse"] for b in _blocks(message) if isinstance(b.get("toolUse"), dict)])
    return result


def check_tool_result_pairing(exchanges: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> tuple[bool, str]:
    """Every toolUseId in response i is answered, in order, by request i+1.

    Returns (ok, sanitized detail). The last request of the pair must end with
    a user message holding exactly one toolResult per toolUse, same IDs, same
    order, each with a status. The final response must contain no toolUse.
    """
    uses = requested_tool_uses(exchanges)
    pairs = 0
    for i, round_uses in enumerate(uses):
        if not round_uses:
            continue
        if i + 1 >= len(exchanges):
            return False, "toolUse in the final response was never answered"
        messages = exchanges[i + 1][0].get("messages")
        if not isinstance(messages, list) or not messages:
            return False, "follow-up request has no messages"
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return False, "follow-up request does not end with a user message"
        blocks = _blocks(last)
        results = [b.get("toolResult") for b in blocks]
        if not blocks or not all(isinstance(r, dict) for r in results):
            return False, "follow-up user message contains non-toolResult blocks"
        if [r.get("toolUseId") for r in results] != [u.get("toolUseId") for u in round_uses]:
            return False, "toolResult IDs do not match toolUse IDs in order"
        if not all(r.get("status") in ("success", "error") for r in results):
            return False, "toolResult without a valid status"
        pairs += len(results)
    return True, f"{pairs} toolUse/toolResult pair(s) matched"


def _tool_result_payloads(exchanges, tool_use_id: str) -> list[dict[str, Any]]:
    for request, _ in exchanges:
        for message in request.get("messages", []) or []:
            for block in _blocks(message):
                result = block.get("toolResult")
                if isinstance(result, dict) and result.get("toolUseId") == tool_use_id:
                    return [c["json"] for c in result.get("content", []) if isinstance(c, dict) and isinstance(c.get("json"), dict)]
    return []


def evaluate_tool_loop(
    report: ValidationReport,
    exchanges: list[tuple[dict[str, Any], dict[str, Any] | None]],
    tool_calls: tuple[Any, ...],
    audit_types: list[str],
) -> set[str]:
    """Add tool-loop checks to ``report``; return the tools exercised successfully."""
    uses = [u for round_uses in requested_tool_uses(exchanges) for u in round_uses]
    requested = [u.get("name") for u in uses]
    report.tools_requested = tuple(dict.fromkeys(n for n in requested if isinstance(n, str) and n in registry.TOOL_NAMES))

    allowlisted = all(isinstance(n, str) and n in registry.TOOL_NAMES for n in requested) and all(
        c.name in registry.TOOL_NAMES for c in tool_calls
    )
    report.add("loop.allowlisted_tools_only", PASS if allowlisted else FAIL,
               f"{len(requested)} requested, {len(tool_calls)} executed")

    ok, detail = check_tool_result_pairing(exchanges)
    if ok and len(tool_calls) != len(uses):
        ok, detail = False, "executed tool count differs from requested toolUse count"
    report.add("loop.tool_use_id_pairing", PASS if ok else FAIL, detail)

    exercised: set[str] = set()
    for name in (order_details.TOOL_NAME, carrier_tracking.TOOL_NAME):
        records = [c for c in tool_calls if c.name == name]
        if not records:
            report.add(f"loop.{name}", NOT_EXERCISED, "model did not request this tool")
        elif all(c.status == "success" for c in records):
            exercised.add(name)
            report.add(f"loop.{name}", PASS, f"requested and executed {len(records)}x")
        else:
            codes = sorted({c.error_code or "error" for c in records if c.status != "success"})
            report.add(f"loop.{name}", FAIL, f"tool returned error ({', '.join(codes)})")

    if carrier_tracking.TOOL_NAME in exercised:
        record = next(c for c in tool_calls if c.name == carrier_tracking.TOOL_NAME)
        payloads = _tool_result_payloads(exchanges, record.tool_use_id)
        status = payloads[0].get("current_delivery_status", {}) if payloads else {}
        verification = status.get("verification") if isinstance(status, dict) else None
        # The synthetic order has fresh carrier data, so the platform marks it VERIFIED.
        report.add("loop.carrier_verification_from_platform",
                   PASS if verification == carrier_tracking.VERIFIED else FAIL,
                   f"verification={verification if verification in (carrier_tracking.VERIFIED, carrier_tracking.UNVERIFIED) else 'missing'}")

    # Event order varies with the model's choices; check counts and the outcome event.
    audited = audit_types.count("tool.succeeded") == sum(1 for c in tool_calls if c.status == "success") and (
        "tool_loop.completed" in audit_types
    )
    report.add("loop.audit_events", PASS if audited else FAIL, f"{len(audit_types)} event(s), in memory only")
    return exercised


def evaluate_probe_response(response: object, tool_name: str) -> tuple[str, str]:
    """Check a forced-toolChoice response without executing the tool."""
    if not isinstance(response, dict):
        return FAIL, "response is not an object"
    if response.get("stopReason") != "tool_use":
        return FAIL, "stopReason is not tool_use"
    message = response.get("output", {}).get("message") if isinstance(response.get("output"), dict) else None
    uses = [b["toolUse"] for b in _blocks(message) if isinstance(b.get("toolUse"), dict)]
    if len(uses) != 1:
        return FAIL, f"expected exactly one toolUse, got {len(uses)}"
    use = uses[0]
    if use.get("name") != tool_name:
        return FAIL, "model called a different tool"
    if not isinstance(use.get("toolUseId"), str) or not use["toolUseId"].strip():
        return FAIL, "toolUse has no toolUseId"
    try:
        registry.validate_named_tool_input(tool_name, use.get("input"))
    except Exception:  # noqa: BLE001 - any validation failure fails the probe
        return FAIL, "toolUse input failed schema validation (tool not executed)"
    return PASS, "forced toolChoice honored; input schema-valid; tool not executed"


def platform_carrier_verified(order_id: str, access_context: AccessContext) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """Platform-owned verification from the synthetic carrier tool (no model)."""
    order = order_details.get_order_details(order_id, access_context=access_context)
    tracking = carrier_tracking.get_carrier_tracking(order_id, access_context=access_context)
    verified = tracking.get("current_delivery_status", {}).get("verification") == carrier_tracking.VERIFIED
    return verified, order, tracking


def build_structured_evidence(order: dict[str, Any], tracking: dict[str, Any]) -> str:
    """Synthetic evidence text from platform-retrieved tool results (data only)."""
    return (
        "[SYNTHETIC DATA - fictional company, retrieved by the platform from synthetic adapters]\n"
        f"order_details ({order_details.TOOL_NAME}):\n{json.dumps(order, indent=2, sort_keys=True)}\n"
        f"carrier_tracking ({carrier_tracking.TOOL_NAME}):\n{json.dumps(tracking, indent=2, sort_keys=True)}\n"
    )


def evaluate_structured(report: ValidationReport, investigation: Any, order_id: str, verified: bool) -> None:
    report.add("structured.schema_validated", PASS, "DelayedDeliveryInvestigation (strict)")
    report.add("structured.requires_human_review", PASS if investigation.requires_human_review is True else FAIL)
    report.add("structured.order_id_matches", PASS if investigation.order_id == order_id else FAIL)
    status = investigation.delivery_status
    allowed = status is DeliveryStatus.UNVERIFIED or (status is DeliveryStatus.VERIFIED and verified)
    report.add("structured.delivery_status_supported", PASS if allowed else FAIL,
               f"model={status.value}, platform_verified={'yes' if verified else 'no'}")


# --- Phases ----------------------------------------------------------------


def run_preflight(report: ValidationReport, client: MeteredClient) -> None:
    """Local fail-closed checks; must not send any Converse request."""
    before = client.request_count
    try:
        run_tool_loop(LOOP_QUESTION, access_context=None, client=client, audit_sink=InMemoryAuditSink())
        report.add("preflight.missing_context_fails_closed", FAIL, "loop ran without an access context")
    except ToolLoopError as exc:
        ok = exc.code == "MissingAuthorization" and client.request_count == before
        report.add("preflight.missing_context_fails_closed", PASS if ok else FAIL, "0 model calls")
    try:
        registry.run_named_tool(carrier_tracking.TOOL_NAME, {"order_id": SYNTHETIC_ORDER_ID},
                                access_context=_OMS_ONLY_CONTEXT)
        report.add("preflight.carrier_permission_denied", FAIL, "carrier read allowed without permission")
    except AccessDeniedError as exc:
        report.add("preflight.carrier_permission_denied", PASS, f"denied ({exc.check} check)")


def run_tool_loop_phase(report: ValidationReport, client: MeteredClient, model_id: str, region: str,
                        policy: RetryPolicy) -> set[str]:
    start = client.mark()
    sink = InMemoryAuditSink()
    try:
        result = run_tool_loop(
            LOOP_QUESTION,
            access_context=SYNTHETIC_CONTEXT,
            client=client,
            model_id=model_id,
            region=region,
            max_tokens=LOOP_MAX_TOKENS,
            max_tool_rounds=LOOP_MAX_TOOL_ROUNDS,
            retry_policy=policy,
            audit_sink=sink,
        )
    except BedrockInvocationError as exc:
        report.add("loop.completed", FAIL, f"code={_error_code(exc)}")
        evaluate_tool_loop(report, client.exchanges[start:], getattr(exc, "tool_calls", ()), sink.types())
        return set()
    report.add("loop.completed", PASS, f"end_turn after {result.rounds} tool round(s); free text, not schema validated")
    return evaluate_tool_loop(report, client.exchanges[start:], result.tool_calls, sink.types())


def run_probe_phase(report: ValidationReport, client: MeteredClient, model_id: str, policy: RetryPolicy,
                    tool_name: str) -> None:
    request = {
        "modelId": model_id,
        "system": [{"text": _PROBE_SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": f"Call {tool_name} for order {SYNTHETIC_ORDER_ID}."}]}],
        "inferenceConfig": {"maxTokens": PROBE_MAX_TOKENS},
        "toolConfig": {"tools": copy.deepcopy(list(registry.TOOL_SPECS)), "toolChoice": {"tool": {"name": tool_name}}},
    }
    try:
        response = converse_with_retry(client, request, policy)
    except BedrockInvocationError as exc:
        report.add(f"probe.tool_choice.{tool_name}", FAIL, f"code={_error_code(exc)}")
        return
    status, detail = evaluate_probe_response(response, tool_name)
    report.add(f"probe.tool_choice.{tool_name}", status, detail)


def run_structured_phase(report: ValidationReport, client: MeteredClient, model_id: str, region: str) -> None:
    verified, order, tracking = platform_carrier_verified(SYNTHETIC_ORDER_ID, SYNTHETIC_CONTEXT)
    try:
        investigation = investigate_delay_structured(
            build_structured_evidence(order, tracking),
            order_id=SYNTHETIC_ORDER_ID,
            carrier_status_verified=verified,
            client=client,
            model_id=model_id,
            region=region,
            max_tokens=STRUCTURED_MAX_TOKENS,
        )
    except InvalidStructuredOutputError as exc:
        report.add("structured.schema_validated", FAIL, f"rejected, fail closed (code={_error_code(exc)})")
        return
    except BedrockInvocationError as exc:
        report.add("structured.schema_validated", FAIL, f"code={_error_code(exc)}")
        return
    evaluate_structured(report, investigation, SYNTHETIC_ORDER_ID, verified)


def run_validation(
    client: Any,
    *,
    model_id: str | None = None,
    region: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    probe: bool = True,
    policy: RetryPolicy | None = None,
) -> ValidationReport:
    """Run all phases against ``client`` (real or mocked); never creates one.

    Scenarios are not retried as a whole. A request-cap stop is reported as
    FAIL for the phase it interrupts; later phases still run but are refused
    by the cap before any request is sent.
    """
    model_id, region = resolve_config(model_id, region)
    policy = policy if policy is not None else live_retry_policy()
    metered = client if isinstance(client, MeteredClient) else MeteredClient(client, max_requests)
    report = ValidationReport(model_id=model_id, region=region)

    run_preflight(report, metered)
    exercised = run_tool_loop_phase(report, metered, model_id, region, policy)
    if probe:
        for name in (order_details.TOOL_NAME, carrier_tracking.TOOL_NAME):
            if name not in exercised:
                run_probe_phase(report, metered, model_id, policy, name)
    run_structured_phase(report, metered, model_id, region)

    report.request_count = metered.request_count
    report.usage = dict(metered.usage)
    return report


def opted_in(environ: dict[str, str], argv: list[str]) -> bool:
    """True only when the env var equals ``OPT_IN_VALUE`` and the flag is present."""
    return environ.get(OPT_IN_ENV) == OPT_IN_VALUE and CONFIRM_FLAG in argv
