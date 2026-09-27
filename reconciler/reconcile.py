"""Orchestration: gather inputs through the ports, run the pure core, execute the action.

Stateless by design (D24/D26): every run reads branches from GitHub and stacks from
CloudFormation afresh, so a skipped or repeated run loses nothing.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

from reconciler.branches import PREVIEW_PREFIX, Group, Main
from reconciler.core import (
    Action,
    EnvPlan,
    ImageLookup,
    Plan,
    desired_env,
    main_env,
    make_plan,
)
from reconciler.ports import (
    BranchSource,
    Deployer,
    ImageRegistry,
    ReconcileError,
    StackDeleter,
    StackInventory,
)
from reconciler.registry import Registry

# How far back to look for a main commit with an image (D26). The newest usually has one;
# a few more cover a merge whose image is still building.
MAIN_LOOKBACK = 20


def build_plan(
    target: Group | Main,
    *,
    trigger: str,
    registry: Registry,
    github: BranchSource,
    images: ImageRegistry | None,
    stacks: StackInventory | None,
) -> Plan:
    """Build the plan for one env. `images`/`stacks` None = --no-aws (SHAs only)."""
    if isinstance(target, Main):
        desired = main_env(registry)
    else:
        prefix = f"{PREVIEW_PREFIX}{target.name}"
        branches = {s.name: github.branches(s.repo, prefix) for s in registry.services}
        desired = desired_env(target.name, registry, branches)

    main_commits = {}
    if isinstance(desired, EnvPlan):
        main_commits = {
            s.name: github.main_commits(s.repo, MAIN_LOOKBACK) for s in registry.services
        }

    status = None
    if stacks is not None:
        status = stacks.env_stacks().get(desired.env)

    return make_plan(
        trigger=trigger,
        desired=desired,
        main_commits=main_commits,
        image_for=_image_lookup(images),
        stack_status=status,
        stacks_checked=stacks is not None,
    )


def _image_lookup(images: ImageRegistry | None) -> ImageLookup | None:
    if images is None:
        return None
    # The ECR repository is named after the service (contract "Images").
    return lambda service, sha: images.image_for(service, sha)


def apply_plan(
    plan: Plan,
    *,
    deployer: Deployer,
    deleter: StackDeleter,
    log: Callable[[str], None] = print,
) -> Action:
    """Execute the plan's action. Refuses plans with errors (conflict, missing images, ...).

    Deletes go straight to CloudFormation (D35): no EnvSpec, no image lookups, no synth,
    so teardown cannot fail because of an image or ECR problem.
    """
    if not plan.ok:
        raise ReconcileError(f"refusing to apply the plan for env '{plan.env}': it has errors")
    if plan.decision is None:
        raise ReconcileError("cannot apply without checking the stack (apply needs AWS)")

    action = plan.decision.action
    if action is Action.NOOP:
        log(f"Nothing to do for env '{plan.env}'.")
        return action

    if action in (Action.DESTROY, Action.DELETE_THEN_CREATE):
        deleter.delete(plan.env)
    if action in (Action.CREATE, Action.UPDATE, Action.DELETE_THEN_CREATE):
        assert plan.resolved is not None  # an EnvPlan always has a resolution
        spec = plan.resolved.to_env_spec()
        with tempfile.TemporaryDirectory(prefix="pe-envspec-") as tmp:
            spec_path = Path(tmp) / f"envspec-{plan.env}.json"
            spec.write(spec_path)
            log(f"EnvSpec ({spec_path}):\n{spec.to_json()}")
            deployer.deploy(plan.env, spec_path)
    return action
