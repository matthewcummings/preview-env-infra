"""CLI: `uv run python -m reconciler plan|apply (--group X | --branch B | --env main)`.

Also `teardown --group X`: an operator command that deletes a preview env's stack even if
branches still exist (e.g. a missed delete event, D21/D29). The reconciler itself only
tears down when no branches remain (D26).

Exit codes:
  0 = done, including ignored branches, no-ops, and waiting for images still being built
  1 = refused or failed (conflict, blocked stack, deploy error, bad config)
  2 = bad usage
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from reconciler.branches import Group, Ignored, Main, parse_branch, validate_group
from reconciler.core import MAIN_ENV, Action, Plan, Teardown, desired_env
from reconciler.ports import (
    BranchSource,
    Deployer,
    ImageRegistry,
    ReconcileError,
    StackDeleter,
    StackInventory,
)
from reconciler.reconcile import apply_plan, build_plan, list_branches
from reconciler.registry import DEFAULT_REGISTRY_PATH, Registry, load_registry
from reconciler.render import (
    render_ignored_markdown,
    render_ignored_text,
    render_markdown,
    render_text,
)

PLACEHOLDER_OWNER = "CHANGE_ME"


@dataclass
class Adapters:
    github: BranchSource
    images: ImageRegistry | None
    stacks: StackInventory | None
    deployer: Deployer | None
    deleter: StackDeleter | None


type AdapterFactory = Callable[[Registry, bool], Adapters]


def real_adapters(registry: Registry, use_aws: bool) -> Adapters:
    # Imported here so `plan --no-aws` never needs boto3 or AWS configuration.
    from reconciler.adapters.github import GitHubBranches

    github = GitHubBranches(registry.github_owner)
    if not use_aws:
        return Adapters(github, None, None, None, None)

    import boto3

    from reconciler.adapters.aws import CloudFormationStacks, EcrImages
    from reconciler.adapters.cdk import CdkRunner

    session = boto3.session.Session()
    if not session.region_name:
        raise ReconcileError("no AWS region configured; set AWS_REGION (or use --no-aws)")
    stacks = CloudFormationStacks(session.client("cloudformation"))
    return Adapters(
        github=github,
        images=EcrImages(session.client("ecr"), session.region_name),
        stacks=stacks,
        deployer=CdkRunner(),
        deleter=stacks,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reconciler",
        description="Work out what a preview env should run, and make it so.",
    )
    parser.add_argument("command", choices=["plan", "apply", "teardown"])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--group", help="reconcile the env for this group")
    target.add_argument("--branch", help="reconcile the env this branch belongs to")
    target.add_argument("--env", choices=[MAIN_ENV], help="reconcile the shared main env")
    parser.add_argument(
        "--no-aws", action="store_true", help="plan only: skip ECR and CloudFormation"
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="append the Markdown plan here (e.g. $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="append env=<env> and action=<action> here (e.g. $GITHUB_OUTPUT)",
    )
    parser.add_argument(
        "--spec-out",
        type=Path,
        help="apply: also write the deployed EnvSpec here (for the smoke test)",
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    return parser


def main(argv: Sequence[str] | None = None, *, adapters: AdapterFactory = real_adapters) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "plan" and args.no_aws:
        parser.error(f"{args.command} needs AWS; --no-aws only works with plan")
    if args.command == "teardown" and args.group is None:
        parser.error("teardown needs --group (the main env is never torn down)")

    summary = _Summary(args.summary_file)
    outputs = _Outputs(args.github_output)

    # Which env? Ignored branches stop here, successfully: no env is not an error (D7).
    if args.group is not None:
        if (problem := validate_group(args.group)) is not None:
            parser.error(problem)
        target: Group | Main = Group(args.group)
        trigger = f"group {args.group}"
    elif args.branch is not None:
        match parse_branch(args.branch):
            case Ignored() as ignored:
                print(render_ignored_text(ignored))
                summary.append(render_ignored_markdown(ignored))
                outputs.write(env="", action="ignored")
                return 0
            case kind:
                target = kind
        trigger = f"branch {args.branch}"
    else:
        target = Main()
        trigger = f"env {args.env}"

    try:
        registry = load_registry(args.registry)
        if registry.github_owner == PLACEHOLDER_OWNER:
            raise ReconcileError(
                f"github_owner is still '{PLACEHOLDER_OWNER}' in {args.registry}. Set it to the "
                "GitHub user or org that owns the service repos, or export "
                "PREVIEW_ENV_GITHUB_OWNER."
            )
        deps = adapters(registry, not args.no_aws)
        if args.command == "teardown":
            assert isinstance(target, Group) and deps.deleter is not None
            return _teardown(target.name, registry, deps, outputs)
        plan = build_plan(
            target,
            trigger=trigger,
            registry=registry,
            github=deps.github,
            images=deps.images,
            stacks=deps.stacks,
        )
        print(render_text(plan), flush=True)
        summary.append(render_markdown(plan))
        outputs.write(env=plan.env, action=_planned_action(plan))
        if not plan.ok:
            return 1
        if args.command == "plan":
            return 0

        assert deps.deployer is not None and deps.deleter is not None and deps.stacks is not None
        action = apply_plan(
            plan, deployer=deps.deployer, deleter=deps.deleter, spec_out=args.spec_out
        )
        result = f"Applied: {action} for env '{plan.env}'."
        if action in (Action.CREATE, Action.UPDATE, Action.DELETE_THEN_CREATE):
            stack_outputs = deps.stacks.outputs(plan.env)
            result += "".join(f"\n  {k}: {v}" for k, v in sorted(stack_outputs.items()))
        print(result)
        summary.append("\n" + "\n".join(f"    {line}" for line in result.splitlines()) + "\n")
        return 0
    except ReconcileError as err:
        return _fail(str(err), summary, outputs, target)
    except Exception as err:
        # botocore raises its own types (no credentials, expired token, ...). Name the
        # failure plainly instead of dumping a traceback on whoever reads the CI log.
        if type(err).__module__.startswith("botocore"):
            message = f"AWS error: {err} (for a plan without AWS, use --no-aws)"
            return _fail(message, summary, outputs, target)
        raise


def _teardown(group: str, registry: Registry, deps: Adapters, outputs: _Outputs) -> int:
    desired = desired_env(group, registry, list_branches(group, registry, deps.github))
    if not isinstance(desired, Teardown):
        print(
            f"WARNING: group '{group}' still has preview branches; the next push to one of "
            "them recreates the env. Delete the branches to keep it gone."
        )
    outputs.write(env=group, action=str(Action.DESTROY))
    assert deps.deleter is not None
    deps.deleter.delete(group)
    print(f"Torn down env '{group}'.")
    return 0


def _planned_action(plan: Plan) -> str:
    """The `action` output: what apply does (or did), so a workflow can branch on it."""
    if not plan.ok:
        return "refused"
    if plan.decision is None:
        return "unknown"  # --no-aws: the stack was not checked
    return str(plan.decision.action)


def _fail(message: str, summary: _Summary, outputs: _Outputs, target: Group | Main) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    summary.append(f"**ERROR:** {message}\n")
    # Written only if the plan never got as far as writing its own outputs.
    outputs.write(env=MAIN_ENV if isinstance(target, Main) else target.name, action="error")
    return 1


class _Summary:
    """Appends to the GitHub job summary file, if one was given."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def append(self, markdown: str) -> None:
        if self.path is not None:
            with self.path.open("a") as f:
                f.write(markdown + "\n")


class _Outputs:
    """Writes `env=` and `action=` to the GitHub step outputs file, if one was given.

    Values: env = the env name ("" for an ignored branch); action = create, update,
    destroy, noop, delete-then-create, wait (images not built yet), refused
    (conflict/errors/blocked), unknown
    (--no-aws), ignored, or error (the run failed before or during apply). The first
    write wins, so a failure during apply still reports the planned action.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.written = False

    def write(self, *, env: str, action: str) -> None:
        if self.path is None or self.written:
            return
        with self.path.open("a") as f:
            f.write(f"env={env}\naction={action}\n")
        self.written = True
