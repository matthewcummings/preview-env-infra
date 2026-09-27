"""Preflight checklist before deploying (D14 `make doctor`): tools, AWS access, CDK bootstrap.

    uv run python scripts/doctor.py

Read-only: it runs `--version` style commands and two AWS read calls (STS
GetCallerIdentity, CloudFormation DescribeStacks on CDKToolkit). Exit code 1 if anything
required is missing, with a hint on how to fix each item.
"""

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, load_registry  # noqa: E402

BOOTSTRAP_STACK = "CDKToolkit"
PLACEHOLDER_OWNER = "CHANGE_ME"

TOOLS = [
    ("uv", "https://docs.astral.sh/uv/getting-started/installation/"),
    ("docker", "https://docs.docker.com/get-docker/ (only needed to build images locally)"),
    ("node", "https://nodejs.org/ (Node 24; the CDK CLI runs on it)"),
    ("npx", "comes with Node.js"),
    ("aws", "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"),
]


@dataclass(frozen=True)
class Result:
    ok: bool
    name: str
    detail: str
    hint: str = ""
    required: bool = True


def check_tools(which: Callable[[str], str | None]) -> list[Result]:
    results = []
    for tool, hint in TOOLS:
        path = which(tool)
        results.append(Result(path is not None, tool, path or "not found", f"install: {hint}"))
    return results


def check_aws(session: Any) -> list[Result]:
    from botocore.exceptions import BotoCoreError, ClientError

    results = []
    region = session.region_name
    results.append(
        Result(True, "region", region)
        if region
        else Result(
            False,
            "region",
            f"not set; the tools default to {DEFAULT_REGION}",
            "set AWS_REGION (or `aws configure set region <region>`) to choose one",
            required=False,
        )
    )
    region = region or DEFAULT_REGION

    try:
        identity = session.client("sts", region_name=region).get_caller_identity()
    except (BotoCoreError, ClientError) as err:
        results.append(
            Result(False, "AWS credentials", str(err), "run `aws configure` or `aws sso login`")
        )
        return results  # the bootstrap check needs working credentials
    results.append(Result(True, "AWS credentials", f"{identity['Arn']} ({identity['Account']})"))

    cfn = session.client("cloudformation", region_name=region)
    hint = f"run `make bootstrap` (npx cdk bootstrap) for {region}"
    try:
        (stack,) = cfn.describe_stacks(StackName=BOOTSTRAP_STACK)["Stacks"]
    except ClientError as err:
        results.append(Result(False, "CDK bootstrap", f"{BOOTSTRAP_STACK} not found: {err}", hint))
        return results
    status = stack["StackStatus"]
    ok = status.endswith("_COMPLETE") and "ROLLBACK" not in status
    results.append(Result(ok, "CDK bootstrap", f"{BOOTSTRAP_STACK} in {region}: {status}", hint))
    return results


def check_github_owner(registry_path: Path) -> Result:
    owner = load_registry(registry_path).github_owner
    if owner == PLACEHOLDER_OWNER:
        return Result(
            False,
            "GitHub owner",
            f"github_owner is still {PLACEHOLDER_OWNER}",
            "export PREVIEW_ENV_GITHUB_OWNER=<your GitHub user or org>, or edit services.yaml",
        )
    source = "PREVIEW_ENV_GITHUB_OWNER" if os.environ.get("PREVIEW_ENV_GITHUB_OWNER") else "services.yaml"
    return Result(True, "GitHub owner", f"{owner} (from {source})")


def render(results: Sequence[Result]) -> str:
    lines = []
    for r in results:
        mark = "ok  " if r.ok else ("FAIL" if r.required else "warn")
        lines.append(f"[{mark}] {r.name}: {r.detail}")
        if r.hint and not r.ok:
            lines.append(f"       -> {r.hint}")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any = None,
    which: Callable[[str], str | None] = shutil.which,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    if session is None:
        import boto3

        session = boto3.Session()
    results = [*check_tools(which), *check_aws(session), check_github_owner(registry_path)]
    print(render(results))
    failed = [r for r in results if r.required and not r.ok]
    print(f"\n{'All good.' if not failed else f'{len(failed)} problem(s) to fix first.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
