"""Print an environment's URL (its stack's Url output). Needs AWS access.

uv run python scripts/env_url.py <env>
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION, env_stack_name  # noqa: E402
from scripts._common import env_url, fail  # noqa: E402


def main(argv: Sequence[str] | None = None, *, session: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("env", help="env name: main or a group")
    args = parser.parse_args(argv)

    if session is None:
        import boto3

        session = boto3.Session()
    cfn = session.client("cloudformation", region_name=session.region_name or DEFAULT_REGION)
    url = env_url(cfn, args.env)
    if url is None:
        return fail(f"no URL for env '{args.env}': is {env_stack_name(args.env)} deployed?")
    print(url)  # alone on stdout, so scripts can capture it
    return 0


if __name__ == "__main__":
    sys.exit(main())
