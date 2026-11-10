"""Manual, opt-in live validation of the Bedrock tool loop and structured output (Step 2.6G).

Makes billable Amazon Bedrock calls. Never run by unit-test discovery or CI
(it is not under tests/ and is not named test_*.py). It refuses to run
unless BOTH the environment variable and the flag are given:

    NORTHSTAR_RUN_LIVE_BEDROCK=1 PYTHONPATH=src \
        python examples/bedrock_live_validation.py --confirm-paid-calls

Optional flags:
    --no-probe          skip the tool-choice probe for tools the loop did not exercise
    --max-requests N    hard cap on Converse attempts (default 10, retries included)

Uses the default AWS credential chain (for example AWS_PROFILE=<name>) and
the model/region defaults in northstar.llm.bedrock_converse, overridable with
NORTHSTAR_BEDROCK_MODEL_ID and NORTHSTAR_BEDROCK_REGION. All data is
SYNTHETIC; OMS and carrier data come from in-memory synthetic adapters.
Read-only: no refunds, reships or order changes. Prints only sanitized check
statuses, request counts and token totals.

Exit codes: 0 PASS, 2 PARTIAL (a tool was not exercised by the loop), 1 FAIL,
3 not opted in.
"""

import argparse
import os
import sys

from northstar.llm.bedrock_converse import BedrockInvocationError, resolve_config
from northstar.llm.bedrock_live_validation import (
    CONFIRM_FLAG,
    DEFAULT_MAX_REQUESTS,
    OPT_IN_ENV,
    OPT_IN_VALUE,
    live_retry_policy,
    opted_in,
    run_validation,
)
from northstar.llm.bedrock_retry import create_client_with_timeouts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Opt-in live Bedrock validation (billable).")
    parser.add_argument(CONFIRM_FLAG, action="store_true", help="confirm that paid Bedrock calls may be made")
    parser.add_argument("--no-probe", action="store_true", help="skip the tool-choice probe")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    args = parser.parse_args(argv)
    if args.max_requests < 1:
        parser.error("--max-requests must be at least 1")

    if not opted_in(dict(os.environ), argv):
        print(
            f"Not run: set {OPT_IN_ENV}={OPT_IN_VALUE} and pass {CONFIRM_FLAG} to make paid Bedrock calls.",
            file=sys.stderr,
        )
        return 3

    model_id, region = resolve_config()
    policy = live_retry_policy()
    try:
        client = create_client_with_timeouts(region, policy)
    except BedrockInvocationError as exc:
        print(f"Could not create the Bedrock client (code={exc.code or 'unknown'}).", file=sys.stderr)
        return 1

    report = run_validation(
        client,
        model_id=model_id,
        region=region,
        max_requests=args.max_requests,
        probe=not args.no_probe,
        policy=policy,
    )
    print(report.render())
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
