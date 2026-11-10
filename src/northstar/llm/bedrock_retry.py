"""Bedrock request timeouts and bounded retries for transient failures (Step 2.6E).

[Proposal] Default values come from the Step 1.4 proposals: NFR-RTY-02
(retry only transient failures), NFR-RTY-03 (at most 3 attempts), NFR-RTY-04
(exponential backoff with full jitter, base 0.2 s, cap 2 s) and the NFR
timeout budgets (5 s per attempt, 15 s per call including retries). All are
configurable and to be tuned once NFR-LAT-04 is measured.

Transient means a Bedrock throttling or service-side error code, or a
transport timeout or connection failure. Everything else is raised at once:
authorization and configuration errors, validation errors, blocked or
truncated output, malformed responses, and any exception that is not a
``BedrockInvocationError``. Tool-level failures (authorization denials,
invalid arguments, unsupported tool names) never reach this module.

botocore's own retries are disabled on clients created here, so these are
the only retries. Not implemented: ``Retry-After`` handling and circuit
breaking (NFR-RTY-04, NFR-RTY-05).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from northstar.llm.bedrock_converse import _RETRYABLE_CODES, BedrockInvocationError, _converse

# botocore transport errors (``BedrockInvocationError.code`` is the class name).
TRANSIENT_TRANSPORT_CODES = frozenset(
    {"ReadTimeoutError", "ConnectTimeoutError", "EndpointConnectionError", "ConnectionClosedError"}
)
TRANSIENT_CODES = frozenset(_RETRYABLE_CODES) | TRANSIENT_TRANSPORT_CODES


def _positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"{name} must be a positive number")


@dataclass(frozen=True)
class RetryPolicy:
    """Timeouts, attempt count, backoff and total budget for one Converse call."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0
    total_budget_seconds: float = 15.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 5.0
    # Injectable for deterministic tests.
    sleep: Callable[[float], None] = field(default=time.sleep, compare=False, repr=False)
    random: Callable[[], float] = field(default=random.random, compare=False, repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, compare=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name in (
            "base_delay_seconds",
            "max_delay_seconds",
            "total_budget_seconds",
            "connect_timeout_seconds",
            "read_timeout_seconds",
        ):
            _positive(getattr(self, name), name)

    def backoff(self, failed_attempt: int) -> float:
        """Full-jitter delay after the ``failed_attempt``-th failure (1-based)."""
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (failed_attempt - 1))
        return self.random() * ceiling


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, BedrockInvocationError) and exc.code in TRANSIENT_CODES


def create_client_with_timeouts(region: str, policy: RetryPolicy) -> Any:
    """bedrock-runtime client with explicit timeouts and botocore retries disabled."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BedrockInvocationError(
            "boto3 is not installed; install it with: pip install -e '.[bedrock]'"
        ) from exc
    config = Config(
        connect_timeout=policy.connect_timeout_seconds,
        read_timeout=policy.read_timeout_seconds,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    return boto3.client("bedrock-runtime", region_name=region, config=config)


# on_failure(attempt, error, will_retry, backoff_seconds)
FailureHook = Callable[[int, BedrockInvocationError, bool, float], None]


def converse_with_retry(
    client: Any, request: dict[str, Any], policy: RetryPolicy, *, on_failure: FailureHook | None = None
) -> dict[str, Any]:
    """Call Converse, retrying transient failures within the policy's bounds.

    Raises the last ``BedrockInvocationError`` when attempts or the budget run
    out, or at once for a non-transient error. The budget is checked before
    each retry; an in-flight call is bounded by the client's timeouts.
    """
    start = policy.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return _converse(client, request)
        except BedrockInvocationError as exc:
            delay = 0.0
            will_retry = False
            if is_transient(exc) and attempt < policy.max_attempts:
                delay = policy.backoff(attempt)
                will_retry = policy.monotonic() - start + delay < policy.total_budget_seconds
            if on_failure is not None:
                on_failure(attempt, exc, will_retry, delay if will_retry else 0.0)
            if not will_retry:
                raise
            policy.sleep(delay)
