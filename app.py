#!/usr/bin/env python3
"""CDK entry point.

    npx cdk synth       # preview-baseline + preview-env-sample (built-in spec)
    npx cdk deploy preview-baseline [-c githubOidcProvider=existing]
    npx cdk deploy preview-env-<env> -c envSpec=<file>  # what the reconciler runs

Stacks are account-agnostic and need no lookups, so `cdk synth` works without AWS
credentials (D1 tier 1). The region comes from the CDK CLI's AWS config, else us-east-1
(D20).
"""

import os
from pathlib import Path

import aws_cdk as cdk

from infra import config
from infra.env_stack import EnvStack
from infra.sample import sample_env_spec
from infra.shared_stack import SharedStack
from reconciler.registry import load_registry
from reconciler.spec import EnvSpec


def main() -> None:
    app = cdk.App()
    registry = load_registry()
    env = cdk.Environment(region=os.environ.get("CDK_DEFAULT_REGION") or config.DEFAULT_REGION)

    # D39: `-c githubOidcProvider=existing` reuses the account's GitHub OIDC provider.
    oidc_mode = app.node.try_get_context("githubOidcProvider") or "create"
    if oidc_mode not in ("create", "existing"):
        raise ValueError(f"githubOidcProvider must be 'create' or 'existing', got {oidc_mode!r}")

    SharedStack(
        app,
        config.SHARED_STACK_NAME,
        registry=registry,
        infra_repo=app.node.try_get_context("infraRepo") or config.DEFAULT_INFRA_REPO,
        create_oidc_provider=oidc_mode == "create",
        env=env,
        description="Preview-envs platform: shared baseline (VPC, ECS, ECR, Aurora, OIDC)",
    )

    spec_path = app.node.try_get_context("envSpec")
    spec = EnvSpec.read(Path(spec_path)) if spec_path else sample_env_spec(registry)
    EnvStack(app, config.env_stack_name(spec.env), spec=spec, registry=registry, env=env)

    app.synth()


if __name__ == "__main__":
    main()
