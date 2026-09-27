"""In-memory fakes for the reconciler's ports, plus shared test data. No network."""

from pathlib import Path

from reconciler.core import Branch
from reconciler.registry import Registry, Service
from reconciler.spec import EnvSpec

REGISTRY = Registry(
    github_owner="acme",
    services=(
        Service("service-a", "service-a", "/a", 8000, "/healthz"),
        Service("service-b", "service-b", "/b", 8000, "/healthz"),
    ),
)

# Readable fake SHAs: the first 7 characters show up in rendered plans.
A_MAIN = "a0a0a0a" + "0" * 33
A_MAIN_OLD = "a1a1a1a" + "1" * 33
B_MAIN = "b0b0b0b" + "0" * 33
A_FEAT = "af1af1a" + "f" * 33
B_FEAT = "bf1bf1b" + "f" * 33
A_FEAT_2 = "af2af2a" + "f" * 33

IMAGE_HOST = "123456789012.dkr.ecr.us-east-1.amazonaws.com"


def image(service: str, tag: str) -> str:
    """Fake digest-pinned URI. `<sha>` and `main-<sha>` tag the same build, so same digest."""
    sha = tag.removeprefix("main-")
    return f"{IMAGE_HOST}/{service}@sha256:{sha[:7]}{'d' * 57}"


class FakeGitHub:
    def __init__(
        self,
        branches: dict[str, list[Branch]] | None = None,
        main: dict[str, list[str]] | None = None,
        history: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        self._branches = branches or {}
        self._history = history or {}  # (repo, branch) -> commits, newest first
        self._main = main or {"service-a": [A_MAIN], "service-b": [B_MAIN]}
        self.calls: list[tuple[str, str]] = []

    def branches(self, repo: str, prefix: str) -> list[Branch]:
        self.calls.append(("branches", repo))
        # Same semantics as GitHub's matching-refs: a plain string prefix.
        return [b for b in self._branches.get(repo, []) if b.name.startswith(prefix)]

    def main_commits(self, repo: str, limit: int) -> list[str]:
        self.calls.append(("main_commits", repo))
        return self._main.get(repo, [])[:limit]

    def branch_commits(self, repo: str, branch: str, limit: int) -> list[str]:
        self.calls.append(("branch_commits", repo))
        return self._history.get((repo, branch), [])[:limit]


class FakeEcr:
    def __init__(self, built: set[tuple[str, str]] | None = None) -> None:
        # (repository, tag) pairs that exist. Default: every tag has an image.
        self.built = built

    def image_for(self, repository: str, tag: str) -> str | None:
        if self.built is None or (repository, tag) in self.built:
            return image(repository, tag)
        return None


class FakeStacks:
    def __init__(self, stacks: dict[str, str] | None = None) -> None:
        self.stacks = stacks or {}

    def env_stacks(self) -> dict[str, str]:
        return dict(self.stacks)

    def outputs(self, env: str) -> dict[str, str]:
        return {"EnvUrl": f"http://{env}.example.com"}


class FakeDeployer:
    """Fake for both Deployer (cdk deploy) and StackDeleter (DeleteStack), so one call log
    shows the order of operations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, EnvSpec | None]] = []

    def deploy(self, env: str, spec_path: Path) -> None:
        self.calls.append(("deploy", env, EnvSpec.read(spec_path)))

    def delete(self, env: str) -> None:
        self.calls.append(("delete", env, None))
