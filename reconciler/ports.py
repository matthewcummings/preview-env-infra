"""The small interfaces between the orchestration and the outside world (D8).

Real adapters live in `reconciler.adapters`; tests pass in-memory fakes.
"""

from pathlib import Path
from typing import Protocol

from reconciler.core import Branch


class ReconcileError(Exception):
    """An expected failure with a message meant for humans (bad config, missing repo, ...)."""


class BranchSource(Protocol):
    def branches(self, repo: str, prefix: str) -> list[Branch]:
        """Branches in `repo` whose name starts with `prefix` (a plain string prefix)."""
        ...

    def main_commits(self, repo: str, limit: int) -> list[str]:
        """The newest `limit` commit SHAs on `main`, newest first."""
        ...

    def branch_commits(self, repo: str, branch: str, limit: int) -> list[str]:
        """The newest `limit` commit SHAs on `branch`, newest first."""
        ...


class ImageRegistry(Protocol):
    def image_for(self, repository: str, tag: str) -> str | None:
        """`<registry>/<repository>@sha256:...` for the image with this tag, or None.

        Tags: `<sha>` for every build, plus `main-<sha>` for builds from main."""
        ...


class StackInventory(Protocol):
    def env_stacks(self) -> dict[str, str]:
        """Existing `preview-env-*` stacks as {env: status}. Deleted stacks are not included."""
        ...

    def outputs(self, env: str) -> dict[str, str]:
        """The env stack's CloudFormation outputs (e.g. its URL), {} if none."""
        ...


class Deployer(Protocol):
    def deploy(self, env: str, spec_path: Path) -> None:
        """Create or update preview-env-<env> from the EnvSpec at `spec_path` (`cdk deploy`)."""
        ...


class StackDeleter(Protocol):
    def delete(self, env: str) -> None:
        """Delete preview-env-<env> and wait until it is gone (D35). Needs no EnvSpec."""
        ...
