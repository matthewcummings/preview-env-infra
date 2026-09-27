"""Deploy the baseline stack (preview-baseline), reusing a GitHub OIDC provider if needed (D39).

    uv run python scripts/deploy_baseline.py [--allow-my-ip]

An AWS account can have only one GitHub OIDC provider. preview-baseline creates it by
default, but if one already exists and preview-baseline doesn't own it, preview-baseline
must import it instead (`-c githubOidcProvider=existing`). How this script decides:

  1. preview-baseline exists: keep doing what it did. If its resources include the provider it
     manages it (create); otherwise it imported one (existing). Switching modes on an
     existing stack would delete the provider or fail on a duplicate, so never switch.
  2. First deploy: existing if the account already has a GitHub provider, else create.

Case 1 needs only CloudFormation reads, so CI (the infra role) never needs IAM access.
--allow-my-ip adds your public IP to the ALB allowlist afterwards (D42, first-time setup).
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION, SHARED_STACK_NAME  # noqa: E402
from reconciler.cli import PLACEHOLDER_OWNER  # noqa: E402
from reconciler.registry import load_registry  # noqa: E402
from scripts import allow_ip  # noqa: E402
from scripts._common import Runner, fail, run  # noqa: E402

OIDC_HOST = "token.actions.githubusercontent.com"
OIDC_RESOURCE_TYPE = "AWS::IAM::OIDCProvider"


def shared_stack_resource_types(cfn: Any) -> set[str] | None:
    """Resource types in preview-baseline, or None if the stack doesn't exist yet."""
    from botocore.exceptions import ClientError

    types: set[str] = set()
    try:
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=SHARED_STACK_NAME):
            types.update(r["ResourceType"] for r in page["StackResourceSummaries"])
    except ClientError as err:
        if "does not exist" in err.response.get("Error", {}).get("Message", ""):
            return None
        raise
    return types


def account_has_github_provider(iam: Any) -> bool:
    providers = iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]
    return any(p["Arn"].endswith(f"oidc-provider/{OIDC_HOST}") for p in providers)


def choose_oidc_mode(cfn: Any, iam_client: Callable[[], Any]) -> tuple[str, str]:
    """('create' | 'existing', why). `iam_client()` is only called on a first deploy."""
    types = shared_stack_resource_types(cfn)
    if types is not None:
        if OIDC_RESOURCE_TYPE in types:
            return "create", f"{SHARED_STACK_NAME} already manages the GitHub OIDC provider"
        return "existing", f"{SHARED_STACK_NAME} was deployed importing an existing provider"
    if account_has_github_provider(iam_client()):
        return "existing", "the account already has a GitHub OIDC provider; importing it"
    return "create", "no GitHub OIDC provider in the account yet; creating one"


def cdk_deploy_command(mode: str) -> list[str]:
    cmd = ["npx", "cdk", "deploy", SHARED_STACK_NAME, "--require-approval", "never"]
    if mode == "existing":
        cmd += ["-c", "githubOidcProvider=existing"]
    return cmd


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any = None,
    runner: Runner = run,
    allow: Callable[[list[str]], int] = allow_ip.main,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--allow-my-ip",
        action="store_true",
        help="after deploying, add your public IP to the ALB allowlist (first-time setup)",
    )
    args = parser.parse_args(argv)

    # The OIDC roles' trust is built from the GitHub owner. Deploying with the placeholder would
    # make every role trust `CHANGE_ME/...` and lock CI out (this happened once).
    owner = load_registry().github_owner
    if owner == PLACEHOLDER_OWNER:
        return fail(
            f"github_owner is still '{PLACEHOLDER_OWNER}'. Set it in services.yaml or export "
            "PREVIEW_ENV_GITHUB_OWNER before deploying: the GitHub OIDC roles trust this owner."
        )

    if session is None:
        import boto3

        session = boto3.Session()
    region = session.region_name or DEFAULT_REGION
    cfn = session.client("cloudformation", region_name=region)

    mode, why = choose_oidc_mode(cfn, lambda: session.client("iam"))
    print(f"Deploying {SHARED_STACK_NAME} to {region}. GitHub OIDC provider: {mode} ({why}).")

    code = runner(cdk_deploy_command(mode))
    if code != 0:
        return fail(f"cdk deploy {SHARED_STACK_NAME} failed (exit code {code})")
    print(f"{SHARED_STACK_NAME} deployed.")

    if args.allow_my_ip:
        print("Adding your public IP to the ALB allowlist (D42)...")
        if allow(["add"]) != 0:
            return fail("deployed, but adding your IP failed; rerun `make allow-ip`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
