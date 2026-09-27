"""The pure core of the reconciler (D8): no GitHub, AWS or subprocess calls in here.

Pipeline for one env:
  1. desired_env / main_env   branches -> which ref each service should run (or teardown/conflict)
  2. resolve_images           refs -> exact SHAs and images, falling back to main (D24/D26)
  3. decide_action            desired vs the env's CloudFormation stack -> what to do
The adapters gather the inputs; everything here is a plain function of them, so the
prompt's scenarios are unit tests.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from infra.config import ECR_MAIN_TAG_PREFIX  # the one definition of the main- tag
from reconciler.branches import MAIN_BRANCH, PREVIEW_PREFIX, Group, parse_branch
from reconciler.registry import Registry
from reconciler.spec import EnvSpec, ServiceSpec

STACK_PREFIX = "preview-env-"
MAIN_ENV = "main"


def stack_name(env: str) -> str:
    return f"{STACK_PREFIX}{env}"


def short(sha: str) -> str:
    return sha[:7]


# --- 1. Desired state from branches -------------------------------------------------


@dataclass(frozen=True)
class Branch:
    """A branch as seen in GitHub: name + head commit."""

    name: str
    sha: str


@dataclass(frozen=True)
class UseBranch:
    """The service runs this preview branch."""

    branch: str
    sha: str


@dataclass(frozen=True)
class UseMain:
    """The service runs its newest built `main` (no branch in the group, D26)."""


type Source = UseBranch | UseMain


@dataclass(frozen=True)
class EnvPlan:
    """The env should exist, with this source per service (registry order)."""

    env: str
    services: Mapping[str, Source]


@dataclass(frozen=True)
class Teardown:
    """No service has a branch in the group any more: the env should not exist (D26)."""

    env: str


@dataclass(frozen=True)
class Conflict:
    """Two or more branches in one repo claim the same group (D7). Refuse; touch nothing."""

    env: str
    claims: Mapping[str, tuple[str, ...]]  # service -> the competing branch names

    def message(self) -> str:
        lines = [f"Group '{self.env}' is ambiguous, so env '{self.env}' was left unchanged:"]
        for service, branches in self.claims.items():
            names = " and ".join(f"'{b}'" for b in branches)
            lines.append(f"  {service}: branches {names} both claim group '{self.env}'.")
        lines.append(
            "A group may have at most one branch per repo. Delete or rename all but one "
            f"(e.g. give one its own group: {PREVIEW_PREFIX}{self.env}-2/...), then rerun."
        )
        return "\n".join(lines)


type Desired = EnvPlan | Teardown | Conflict


def desired_env(
    group: str, registry: Registry, branches_by_service: Mapping[str, Sequence[Branch]]
) -> Desired:
    """What env `group` should run, from the branches currently in each service's repo.

    `branches_by_service` may contain unrelated branches (e.g. `preview/checkout-wip/...`
    when asking about `checkout`); only branches whose parsed group is exactly `group` count.
    Every registered service must be present: a missing key means we never looked, and
    silently treating it as "no branch -> main" could hide a bug.
    """
    missing = [s.name for s in registry.services if s.name not in branches_by_service]
    if missing:
        raise ValueError(f"no branch listing for services {missing}")

    sources: dict[str, Source] = {}
    claims: dict[str, tuple[str, ...]] = {}
    for service in registry.services:
        matches = sorted(
            (b for b in branches_by_service[service.name] if parse_branch(b.name) == Group(group)),
            key=lambda b: b.name,
        )
        if len(matches) > 1:
            claims[service.name] = tuple(b.name for b in matches)
        elif matches:
            sources[service.name] = UseBranch(matches[0].name, matches[0].sha)
        else:
            sources[service.name] = UseMain()

    if claims:
        return Conflict(group, claims)
    if not any(isinstance(src, UseBranch) for src in sources.values()):
        return Teardown(group)
    return EnvPlan(group, sources)


def main_env(registry: Registry) -> EnvPlan:
    """The shared env: every service on main. It is never torn down."""
    return EnvPlan(MAIN_ENV, {s.name: UseMain() for s in registry.services})


# --- 2. Images ------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedService:
    service: str
    matched: UseBranch | None  # the group's branch in this repo, if any (even if not used)
    ref: str  # what actually runs: the branch name or "main"
    sha: str  # the commit the image was built from (what /version will report)
    image: str | None  # repo@sha256:... ; None when images were not checked (--no-aws)
    note: str | None = None  # why this differs from the obvious choice, if it does

    @property
    def fell_back(self) -> bool:
        return self.matched is not None and self.ref == MAIN_BRANCH


@dataclass(frozen=True)
class ResolvedEnv:
    env: str
    services: tuple[ResolvedService, ...]
    images_checked: bool
    errors: tuple[str, ...] = field(default=())
    # Services with no image yet at all (e.g. first-time setup, before their first CI build).
    # Not an error: the env deploys once the builds land and signal again (D24).
    waiting: tuple[str, ...] = field(default=())

    def to_env_spec(self) -> EnvSpec:
        if self.errors:
            raise ValueError(f"env '{self.env}' has unresolved services: {list(self.errors)}")
        if self.waiting:
            raise ValueError(f"env '{self.env}' is waiting for images: {list(self.waiting)}")
        if not self.images_checked:
            raise ValueError("images were not checked (--no-aws); cannot build an EnvSpec")
        return EnvSpec(
            env=self.env,
            services={
                s.service: ServiceSpec(ref=s.ref, sha=s.sha, image=s.image or "")
                for s in self.services
            },
        )


# image_for(service, tag) -> image URI pinned by digest, or None if no image has that tag.
type ImageLookup = Callable[[str, str], str | None]


def main_image_tag(sha: str) -> str:
    """The ECR tag of an image built from main: `main-<sha>`.

    Branch images are tagged `<sha>` only; main builds get both. Looking main up by the
    `main-` tag matters with merge commits: main's history also contains feature-branch
    commits, whose `<sha>` images were built from preview branches, not from main.
    """
    return f"{ECR_MAIN_TAG_PREFIX}{sha}"


def resolve_images(
    plan: EnvPlan,
    *,
    main_commits: Mapping[str, Sequence[str]],
    image_for: ImageLookup | None,
    branch_commits: Mapping[str, Sequence[str]] | None = None,
) -> ResolvedEnv:
    """Pin every service to an exact SHA and image.

    - `main` means the newest main commit with a `main-<sha>` image (D26): right after a
      merge the new main image may still be building, and the previous one keeps the env
      working.
    - A branch whose head has no image yet (its build is still running, D24) runs the newest
      earlier commit of that same branch that has one, and the note says so. Staying on the
      branch matters: the env's database may already hold the branch's migrations, which
      main's code wouldn't know. The dispatch after the build finishes corrects the env.
    - Only if none of the branch's recent commits has an image does it fall back to main.
    - `image_for=None` (no AWS): SHAs only; main = the head of main, no fallback checks.

    `main_commits[service]` and `branch_commits[service]` (the matched branch's recent
    commits) are newest first. Lookups are lazy: stop at the first image found.
    """
    branch_commits = branch_commits or {}
    resolved: list[ResolvedService] = []
    errors: list[str] = []
    waiting: list[str] = []

    for service, source in plan.services.items():
        match source:
            case UseBranch(branch, sha):
                if image_for is None:
                    resolved.append(ResolvedService(service, source, branch, sha, None))
                    continue
                candidates = [sha, *(c for c in branch_commits.get(service, ()) if c != sha)]
                built = _newest_branch_build(service, candidates, image_for)
                if built is not None:
                    built_sha, image = built
                    note = None
                    if built_sha != sha:
                        note = (
                            f"head {short(sha)} not built yet -> using {short(built_sha)}, the "
                            "newest built commit on this branch; the env updates when the "
                            "build finishes"
                        )
                    resolved.append(
                        ResolvedService(service, source, branch, built_sha, image, note)
                    )
                    continue
                main = _newest_main_build(service, main_commits.get(service, ()), image_for)
                if isinstance(main, NoImage):
                    waiting.append(
                        f"{service}: no image for {branch} @ {short(sha)} yet, and {main.reason}"
                    )
                    continue
                if isinstance(main, str):
                    errors.append(f"{service}: {main}")
                    continue
                main_sha, main_image, main_note = main
                note = (
                    f"no image yet for {short(sha)} or the branch's last {len(candidates)} "
                    f"commit(s) -> using main @ {short(main_sha)}; the env updates when the "
                    "build finishes"
                )
                if main_note:
                    note += f" ({main_note})"
                resolved.append(
                    ResolvedService(service, source, MAIN_BRANCH, main_sha, main_image, note)
                )
            case UseMain():
                main = _newest_main_build(service, main_commits.get(service, ()), image_for)
                if isinstance(main, NoImage):
                    waiting.append(f"{service}: {main.reason}")
                    continue
                if isinstance(main, str):
                    errors.append(f"{service}: {main}")
                    continue
                main_sha, main_image, main_note = main
                resolved.append(
                    ResolvedService(service, None, MAIN_BRANCH, main_sha, main_image, main_note)
                )

    return ResolvedEnv(
        plan.env, tuple(resolved), image_for is not None, tuple(errors), tuple(waiting)
    )


@dataclass(frozen=True)
class NoImage:
    """No image exists yet for the commits we looked at: wait for CI, don't fail."""

    reason: str


def _newest_branch_build(
    service: str, commits: Sequence[str], image_for: ImageLookup
) -> tuple[str, str] | None:
    """(sha, image) for the newest of these branch commits with a `<sha>` image, or None."""
    for sha in commits:
        image = image_for(service, sha)
        if image is not None:
            return sha, image
    return None


def _newest_main_build(
    service: str, commits: Sequence[str], image_for: ImageLookup | None
) -> tuple[str, str | None, str | None] | NoImage | str:
    """(sha, image, note) for the newest main commit with an image; NoImage if none of them
    has one yet; or an error message."""
    if not commits:
        return "no commits found on main"
    if image_for is None:
        return commits[0], None, None
    for index, sha in enumerate(commits):
        image = image_for(service, main_image_tag(sha))
        if image is not None:
            note = None
            if index:
                note = (
                    f"newest main {short(commits[0])} not built yet; "
                    f"using {short(sha)}, the newest main with an image"
                )
            return sha, image, note
    return NoImage(f"no image built yet for any of the last {len(commits)} main commits")


# --- 3. Desired vs actual -------------------------------------------------------------


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"
    NOOP = "noop"
    DELETE_THEN_CREATE = "delete-then-create"
    BLOCKED = "blocked"
    WAIT = "wait"


# A failed first create leaves the stack in ROLLBACK_COMPLETE: it holds nothing and can
# only be deleted, so delete it and create again (D14).
DELETE_BEFORE_CREATE = frozenset({"ROLLBACK_COMPLETE"})
# Stacks CloudFormation cannot update until a human intervenes. Deploying would just fail
# with a less clear error, so refuse up front (D14: fail clearly).
NEEDS_HUMAN = frozenset(
    {"ROLLBACK_FAILED", "UPDATE_ROLLBACK_FAILED", "DELETE_FAILED", "IMPORT_ROLLBACK_FAILED"}
)


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide_action(desired: EnvPlan | Teardown, stack_status: str | None) -> Decision:
    """Compare the desired env with its stack's status (None = the stack does not exist).

    Updates always go through `cdk deploy`: it is idempotent, and CloudFormation reports
    "no changes" itself, so the reconciler keeps no record of what it last deployed.
    """
    if isinstance(desired, Teardown):
        if stack_status is None:
            return Decision(Action.NOOP, "no branches and no stack: nothing to do")
        return Decision(Action.DESTROY, "no service has a branch in this group any more")

    if stack_status is None:
        return Decision(Action.CREATE, "stack does not exist yet")
    if stack_status in DELETE_BEFORE_CREATE:
        return Decision(
            Action.DELETE_THEN_CREATE,
            f"stack is {stack_status} (its first create failed); delete it, then create",
        )
    if stack_status in NEEDS_HUMAN:
        return Decision(
            Action.BLOCKED,
            f"stack is {stack_status}, which CloudFormation cannot update; "
            "fix or delete the stack by hand, then rerun",
        )
    return Decision(Action.UPDATE, f"stack exists ({stack_status})")


# --- The whole plan for one run -------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    env: str
    trigger: str  # what asked for this run, e.g. "branch preview/checkout/api"
    desired: Desired
    resolved: ResolvedEnv | None  # set when desired is an EnvPlan
    stack_status: str | None  # None = no stack (only meaningful if stacks_checked)
    stacks_checked: bool
    decision: Decision | None  # None for conflicts, and when stacks were not checked

    @property
    def stack(self) -> str:
        return stack_name(self.env)

    @property
    def errors(self) -> tuple[str, ...]:
        errs: list[str] = []
        if isinstance(self.desired, Conflict):
            errs.append(self.desired.message())
        if self.resolved is not None:
            errs.extend(self.resolved.errors)
        if self.decision is not None and self.decision.action is Action.BLOCKED:
            errs.append(self.decision.reason)
        return tuple(errs)

    @property
    def ok(self) -> bool:
        return not self.errors


def make_plan(
    *,
    trigger: str,
    desired: Desired,
    main_commits: Mapping[str, Sequence[str]],
    image_for: ImageLookup | None,
    stack_status: str | None,
    stacks_checked: bool,
    branch_commits: Mapping[str, Sequence[str]] | None = None,
) -> Plan:
    """Combine the three steps into one Plan (still pure)."""
    resolved = None
    if isinstance(desired, EnvPlan):
        resolved = resolve_images(
            desired,
            main_commits=main_commits,
            image_for=image_for,
            branch_commits=branch_commits,
        )
    decision = None
    if stacks_checked and not isinstance(desired, Conflict):
        decision = decide_action(desired, stack_status)
        if resolved is not None and resolved.waiting and decision.action is not Action.BLOCKED:
            decision = Decision(
                Action.WAIT,
                "waiting for images to be built; the env deploys when those builds finish",
            )
    return Plan(
        env=desired.env,
        trigger=trigger,
        desired=desired,
        resolved=resolved,
        stack_status=stack_status,
        stacks_checked=stacks_checked,
        decision=decision,
    )
