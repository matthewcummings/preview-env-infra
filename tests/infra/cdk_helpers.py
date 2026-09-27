"""Helpers for CDK template tests: build specs and synthesize stacks."""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infra.env_stack import EnvStack
from infra.shared_stack import SharedStack
from reconciler.registry import Registry
from reconciler.spec import EnvSpec, ServiceSpec

REGION = cdk.Environment(region="us-east-1")
CDK_JSON = Path(__file__).resolve().parents[2] / "cdk.json"


def new_app() -> cdk.App:
    """An App with cdk.json's context (feature flags), so tests match `cdk synth`."""
    return cdk.App(context=json.loads(CDK_JSON.read_text())["context"])


def make_spec(registry: Registry, env: str) -> EnvSpec:
    return EnvSpec(
        env=env,
        services={
            svc.name: ServiceSpec(
                ref="main" if env == "main" else f"preview/{env}/x",
                sha="a" * 40,
                image=f"111111111111.dkr.ecr.us-east-1.amazonaws.com/{svc.name}@sha256:{'b' * 64}",
            )
            for svc in registry.services
        },
    )


def synth_shared(registry: Registry, *, create_oidc_provider: bool = True) -> Template:
    app = new_app()
    stack = SharedStack(
        app,
        "preview-baseline",
        registry=registry,
        infra_repo="infra-repo",
        create_oidc_provider=create_oidc_provider,
        env=REGION,
    )
    return Template.from_stack(stack)


def synth_env(registry: Registry, spec: EnvSpec) -> Template:
    app = new_app()
    stack = EnvStack(app, f"preview-env-{spec.env}", spec=spec, registry=registry, env=REGION)
    return Template.from_stack(stack)
