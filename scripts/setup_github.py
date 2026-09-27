"""Wire the GitHub repos to AWS after preview-baseline is deployed: repo variables and one secret.

    uv run python scripts/setup_github.py [--dry-run]

Uses the `gh` CLI (you must be logged in: `gh auth login`). Sets:
  - each service repo:  AWS_REGION, AWS_ROLE_ARN (its own push-only role, D11),
                        INFRA_REPO (<owner>/<infra repo>), and the INFRA_DISPATCH_TOKEN secret
  - the infra repo:     AWS_REGION, AWS_DEPLOY_ROLE_ARN (the infra role, D32)

Role ARNs come from preview-baseline's stack outputs. The secret's value comes from the
INFRA_DISPATCH_TOKEN environment variable and is passed to `gh` on stdin, so it never
appears in output or in the process list. --dry-run prints the commands without running
them.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_INFRA_REPO, DEFAULT_REGION, SHARED_STACK_NAME  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, Registry, load_registry  # noqa: E402
from scripts._common import REPO_ROOT, cdk_output_id, fail, stack_outputs  # noqa: E402

TOKEN_ENV = "INFRA_DISPATCH_TOKEN"
PLACEHOLDER_OWNER = "CHANGE_ME"

TOKEN_HELP = """\
{name} is not set, so the dispatch secret was not configured. Create it once:

  1. https://github.com/settings/personal-access-tokens/new (fine-grained token)
  2. Resource owner: {owner}. Expiration: covers the review period.
  3. Repository access: "Only select repositories": {repos}
  4. Repository permissions: Contents: Read and write (write is what lets the service
     repos send repository_dispatch to {infra}; read covers the service repos).
     Metadata: Read-only is added automatically.
  5. Then: export {name}=<the token> and rerun this script.
"""


@dataclass(frozen=True)
class Command:
    args: list[str]
    stdin: str | None = None  # secret values go here, never into args

    def display(self) -> str:
        shown = " ".join(self.args)
        return f"{shown}  (value from ${TOKEN_ENV})" if self.stdin is not None else shown


def infra_repo_name() -> str:
    context = json.loads((REPO_ROOT / "cdk.json").read_text()).get("context", {})
    return context.get("infraRepo") or DEFAULT_INFRA_REPO


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

    commands = []
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
        return fail("set PE_GITHUB_OWNER (or github_owner in services.yaml) first")

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
        repos = ", ".join([infra_repo, *(s.repo for s in registry.services)])
        print()
        print(
            TOKEN_HELP.format(
                name=TOKEN_ENV,
                owner=registry.github_owner,
                repos=repos,
                infra=f"{registry.github_owner}/{infra_repo}",
            )
        )
        return 1
    print("GitHub repos configured." if not args.dry_run else "Dry run: nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
