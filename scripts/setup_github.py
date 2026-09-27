"""Wire the GitHub repos to AWS after preview-baseline is deployed: repo variables and secrets.

    uv run python scripts/setup_github.py [--dry-run]

Uses the `gh` CLI (you must be logged in: `gh auth login`). Sets:
  - every repo:         "Automatically delete head branches" on, so merging a preview
                        branch deletes it and tears its env down (D7, D23)
  - each service repo:  AWS_REGION, AWS_ROLE_ARN (its own push-only role, D11),
                        INFRA_REPO (<owner>/<infra repo>), and the INFRA_DISPATCH_TOKEN secret
  - the infra repo:     AWS_REGION, AWS_DEPLOY_ROLE_ARN (the infra role, D32), and the
                        PREVIEW_COMMENT_TOKEN secret

Role ARNs come from preview-baseline's stack outputs. Each secret's value comes from the
environment variable of the same name and is passed to `gh` on stdin, so it never appears
in output or in the process list. A missing token: everything else is still set, then the
instructions for creating it are printed and the exit code is 1. --dry-run prints the
commands without running them.

Two tokens, not one: a fine-grained token grants the same permissions on every repo it
selects, so one token for both jobs would need both permissions everywhere.
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
COMMENT_TOKEN_ENV = "PREVIEW_COMMENT_TOKEN"
PLACEHOLDER_OWNER = "CHANGE_ME"

TOKEN_HELP = """\
Not set: {missing}. Create each missing token once at
https://github.com/settings/personal-access-tokens/new (fine-grained token), then
export it and rerun this script. Two tokens because a fine-grained token grants the same
permissions on every repo it selects. For each: Resource owner: {owner}. Expiration: 30 days.
Metadata: Read-only is added automatically. Nothing else.
"""

DISPATCH_TOKEN_HELP = """\
{name}: the service repos' CI uses it only to signal {infra}
that a branch changed.
  Repository access: "Only select repositories": ONLY {infra}
  Repository permissions: Contents: Read and write (needed to send repository_dispatch).
  export {name}=<the token>
"""

COMMENT_TOKEN_HELP = """\
{name}: {infra}'s reconcile workflow uses it only to post
the preview URL on service-repo pull requests.
  Repository access: "Only select repositories": ONLY {services}
  Repository permissions: Pull requests: Read and write.
  export {name}=<the token>
"""

TOKEN_HELP_FOOTER = """\
The reconciler reads the service repos with the infra workflow's own GITHUB_TOKEN, so
neither token needs more than this.
"""


@dataclass(frozen=True)
class Command:
    args: list[str]
    stdin: str | None = None  # secret values go here, never into args
    stdin_from: str = ""  # the env var the secret came from, for display

    def display(self) -> str:
        shown = " ".join(self.args)
        return f"{shown}  (value from ${self.stdin_from})" if self.stdin is not None else shown


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
    comment_token: str | None = None,
) -> list[Command]:
    owner = registry.github_owner
    infra_full = f"{owner}/{infra_repo}"

    def var(repo: str, name: str, value: str) -> Command:
        return Command(["gh", "variable", "set", name, "--repo", repo, "--body", value])

    def secret(repo: str, name: str, value: str) -> Command:
        return Command(["gh", "secret", "set", name, "--repo", repo], stdin=value, stdin_from=name)

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
            commands.append(secret(repo, TOKEN_ENV, token))
    commands += [
        var(infra_full, "AWS_REGION", region),
        var(infra_full, "AWS_DEPLOY_ROLE_ARN", infra_role),
    ]
    if comment_token is not None:
        commands.append(secret(infra_full, COMMENT_TOKEN_ENV, comment_token))
    return commands


def token_help(registry: Registry, infra_repo: str, missing: Sequence[str]) -> str:
    """Instructions for creating the missing token(s): exact repos, one permission each."""
    owner = registry.github_owner
    infra = f"{owner}/{infra_repo}"
    services = ", ".join(f"{owner}/{s.repo}" for s in registry.services)
    parts = [TOKEN_HELP.format(missing=", ".join(missing), owner=owner)]
    if TOKEN_ENV in missing:
        parts.append(DISPATCH_TOKEN_HELP.format(name=TOKEN_ENV, infra=infra))
    if COMMENT_TOKEN_ENV in missing:
        parts.append(
            COMMENT_TOKEN_HELP.format(name=COMMENT_TOKEN_ENV, infra=infra, services=services)
        )
    parts.append(TOKEN_HELP_FOOTER)
    return "\n".join(parts)


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
    comment_token = os.environ.get(COMMENT_TOKEN_ENV) or None
    infra_repo = infra_repo_name()
    commands = build_commands(
        registry=registry,
        region=region,
        infra_repo=infra_repo,
        service_roles=service_roles,
        infra_role=infra_role,
        token=token,
        comment_token=comment_token,
    )

    for command in commands:
        print(f"{'(dry run) ' if args.dry_run else ''}$ {command.display()}", flush=True)
        if not args.dry_run and runner(command) != 0:
            return fail(f"`{' '.join(command.args[:4])}` failed; is `gh auth login` done?")

    missing = [
        name
        for name, value in ((TOKEN_ENV, token), (COMMENT_TOKEN_ENV, comment_token))
        if not value
    ]
    if missing:
        print()
        print(token_help(registry, infra_repo, missing))
        return 1
    print("GitHub repos configured." if not args.dry_run else "Dry run: nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
