"""Fixtures for CDK template tests."""

import pytest

from reconciler.registry import Registry, Service, load_registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    return load_registry()


@pytest.fixture(scope="session")
def three_service_registry() -> Registry:
    """Proves nothing hardcodes A and B (D25)."""
    return Registry(
        github_owner="octo",
        services=tuple(
            Service(
                name=f"svc-{x}",
                repo=f"repo-{x}",
                path_prefix=f"/{x}",
                port=8000,
                health_path="/healthz",
            )
            for x in ("x", "y", "z")
        ),
    )
