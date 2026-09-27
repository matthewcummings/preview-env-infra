"""Wire the GitHub repos to AWS after preview-baseline is deployed: repo variables and one secret.

    uv run python scripts/setup_github.py [--dry-run]

Uses the `gh` CLI (you must be logged in: `gh auth login`). Sets:
  - every repo:         "Automatically delete head branches" on, so merging a preview
                        branch deletes it and tears its env down (D7, D23)
  - each service repo:  AWS_REGION, AWS_ROLE_ARN (its own push-only role, D11),
                        INFRA_REPO (<owner>/<infra repo>), and the INFRA_DISPATCH_TOKEN secret
  - the infra repo:     AWS_REGION, AWS_DEPLOY_ROLE_ARN (the infra role, D32)

Role ARNs come from preview-baseline's stack outputs. The secret's value comes from the
INFRA_DISPATCH_TOKEN environment variable and is passed to `gh` on stdin, so it never
appears in output or in the process list. --dry-run prints the commands without running
them.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION, SHARED_STACK_NAME  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, Registry, load_registry  # noqa: E402
from scripts._common import (  # noqa: E402
    cdk_output_id,
    fail,
    infra_repo_name,
    stack_outputs,
)

TOKEN_ENV = "INFRA_DISPATCH_TOKEN"
PLACEHOLDER_OWNER = "CHANGE_ME"

TOKEN_HELP = """\
{name} is not set, so the dispatch secret was not configured. Create it once:

  1. https://github.com/settings/personal-access-tokens/new (fine-grained token)
  2. Resource owner: {owner}. Expiration: 30 days.
  3. Repository access: "Only select repositories": ONLY {infra}
  4. Repository permissions: Contents: Read and write (needed to send repository_dispatch).
     Metadata: Read-only is added automatically. Nothing else.
  5. Then: export {name}=<the token> and rerun this script.

The service repos' CI uses this token only to signal {infra}; the reconciler reads the
service repos with the infra workflow's own GITHUB_TOKEN, so the token needs no access to them.
"""


@dataclass(frozen=True)
class Command:
    args: list[str]
    stdin: str | None = None  # secret values go here, never into args

    def display(self) -> str:
        shown = " ".join(self.args)
        return f"{shown}  (value from ${TOKEN_ENV})" if self.stdin is not None else shown


def role_arns(outputs: dict[str, str], registry: Registry) -> tuple[dict[str, str], str]:
    """(service -> role ARN, infra role ARN) from preview-baseline's outputs (github_oidc.py)."""
    missing = []
    services = {}
    for service in registry.services:
        key = cdk_output_id("GithubOidc", f"{service.name}-RoleArn")
        if key in outputs:
            services[service.name] = outputs[key]
        else:
            missing.append(key)
    infra_key = cdk_output_id("GithubOidc", "InfraRoleArn")
    if infra_key not in outputs:
        missing.append(infra_key)
    if missing:
        raise KeyError(f"{SHARED_STACK_NAME} outputs are missing {missing}")
    return services, outputs[infra_key]


def build_commands(
    *,
    registry: Registry,
    region: str,
    infra_repo: str,
    service_roles: dict[str, str],
    infra_role: str,
    token: str | None,
) -> list[Command]:
    owner = registry.github_owner
    infra_full = f"{owner}/{infra_repo}"

    def var(repo: str, name: str, value: str) -> Command:
        return Command(["gh", "variable", "set", name, "--repo", repo, "--body", value])

    # Merging a branch then deletes it, so "merged" and "deleted" are the same event and
    # teardown only has to handle deletion (D7). Harmless on the infra repo; set for consistency.
    repos = [*(f"{owner}/{s.repo}" for s in registry.services), infra_full]
    commands = [Command(["gh", "repo", "edit", r, "--delete-branch-on-merge"]) for r in repos]
    for service in registry.services:
        repo = f"{owner}/{service.repo}"
        commands += [
            var(repo, "AWS_REGION", region),
            var(repo, "AWS_ROLE_ARN", service_roles[service.name]),
            var(repo, "INFRA_REPO", infra_full),
        ]
        if token is not None:
            commands.append(
                Command(["gh", "secret", "set", TOKEN_ENV, "--repo", repo], stdin=token)
            )
    commands += [
        var(infra_full, "AWS_REGION", region),
        var(infra_full, "AWS_DEPLOY_ROLE_ARN", infra_role),
    ]
    return commands


def token_help(owner: str, infra_repo: str) -> str:
    return TOKEN_HELP.format(name=TOKEN_ENV, owner=owner, infra=f"{owner}/{infra_repo}")


def run_command(command: Command) -> int:
    return subprocess.run(command.args, input=command.stdin, text=True, check=False).returncode


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any = None,
    runner: Callable[[Command], int] = run_command,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the gh commands only")
    args = parser.parse_args(argv)

    registry = load_registry(registry_path)
    if registry.github_owner == PLACEHOLDER_OWNER:
        return fail("set PREVIEW_ENV_GITHUB_OWNER (or github_owner in services.yaml) first")

    if session is None:
        import boto3

        session = boto3.Session()
    region = session.region_name or DEFAULT_REGION
    outputs = stack_outputs(session.client("cloudformation", region_name=region), SHARED_STACK_NAME)
    if outputs is None:
        return fail(f"{SHARED_STACK_NAME} is not deployed in {region}; run `make deploy-baseline`")
    try:
        service_roles, infra_role = role_arns(outputs, registry)
    except KeyError as err:
        return fail(str(err))

    token = os.environ.get(TOKEN_ENV) or None
    infra_repo = infra_repo_name()
    commands = build_commands(
        registry=registry,
        region=region,
        infra_repo=infra_repo,
        service_roles=service_roles,
        infra_role=infra_role,
        token=token,
    )

    for command in commands:
        print(f"{'(dry run) ' if args.dry_run else ''}$ {command.display()}", flush=True)
        if not args.dry_run and runner(command) != 0:
            return fail(f"`{' '.join(command.args[:4])}` failed; is `gh auth login` done?")

    if token is None:
        print()
        print(token_help(registry.github_owner, infra_repo))
        return 1
    print("GitHub repos configured." if not args.dry_run else "Dry run: nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
