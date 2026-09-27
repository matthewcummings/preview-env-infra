"""Branch naming convention (D7): which env, if any, a branch belongs to.

`preview/<group>[/<description>]` joins env `<group>`. `main` is the shared env.
Everything else gets no env: bots quietly, anything else loudly with rename help.
"""

import re
from dataclasses import dataclass

PREVIEW_PREFIX = "preview/"
MAIN_BRANCH = "main"
RESERVED_GROUPS = frozenset({MAIN_BRANCH})
BOT_PREFIXES = ("dependabot/", "renovate/")

# Strict validation, no normalization (contract "Naming"): normalizing would let
# `Foo` and `foo` silently share an env.
GROUP_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")
GROUP_RULE = (
    "<group> must be 1-20 characters of a-z, 0-9 and '-', start and end with a letter "
    "or digit, and must not be 'main'"
)


@dataclass(frozen=True)
class Group:
    """The branch joins the preview env named `name`."""

    name: str


@dataclass(frozen=True)
class Main:
    """The branch is `main`, which feeds the shared `main` env."""


@dataclass(frozen=True)
class Ignored:
    """No env for this branch. `quiet` is True for bot branches, which need no explanation."""

    branch: str
    reason: str
    quiet: bool = False


type BranchKind = Group | Main | Ignored


def parse_branch(name: str) -> BranchKind:
    """Classify a branch name. Accepts a bare name or a full `refs/heads/...` ref."""
    name = name.removeprefix("refs/heads/")
    if name == MAIN_BRANCH:
        return Main()
    if name.startswith(BOT_PREFIXES):
        return Ignored(name, "bot branch (dependabot/renovate) - bots never get preview envs", True)
    if not name.startswith(PREVIEW_PREFIX):
        return Ignored(name, f"does not start with '{PREVIEW_PREFIX}'")

    group = name.removeprefix(PREVIEW_PREFIX).split("/", 1)[0]
    if not group:
        return Ignored(name, "has no group name after 'preview/'")
    if group in RESERVED_GROUPS:
        return Ignored(name, f"group '{group}' is reserved for the shared env")
    if not GROUP_RE.match(group):
        return Ignored(name, f"group '{group}' is not a valid group name ({GROUP_RULE})")
    return Group(group)


def validate_group(group: str) -> str | None:
    """Return None if `group` is a valid group name, else the reason it is not."""
    if group in RESERVED_GROUPS:
        return f"group '{group}' is reserved for the shared env"
    if not GROUP_RE.match(group):
        return f"group '{group}' is not a valid group name ({GROUP_RULE})"
    return None


def suggest_group(branch: str) -> str | None:
    """Best-effort hint for a rename (only ever shown to humans, never applied).

    Uses the last path segment, e.g. `feature/Cart_API` -> `cart-api`.
    """
    last = branch.removeprefix(PREVIEW_PREFIX).rstrip("/").split("/")[-1]
    candidate = re.sub(r"[^a-z0-9-]+", "-", last.lower()).strip("-")[:20].strip("-")
    if candidate and validate_group(candidate) is None:
        return candidate
    return None


def rename_help(ignored: Ignored) -> str:
    """The loud explanation for a non-conforming branch (D7): why, the rule, how to rename."""
    suggestion = suggest_group(ignored.branch) or "<group>"
    new = f"{PREVIEW_PREFIX}{suggestion}"
    return "\n".join(
        [
            f"Branch '{ignored.branch}' is ignored: no preview env.",
            f"Reason: {ignored.reason}.",
            "",
            f"To get a preview env, name the branch {PREVIEW_PREFIX}<group>[/<description>], where",
            f"{GROUP_RULE}. Branches in other repos with the same <group> share the env.",
            "For example:",
            f"  git branch -m {ignored.branch} {new}",
            f"  git push -u origin {new} && git push origin --delete {ignored.branch}",
        ]
    )
