"""`preview-env-<env>`: one environment, built from an EnvSpec (resolved by the reconciler, D8).

CDK decides nothing here: the spec says which image each service runs. The only choice
made in this file is which database component the env gets (D4, D40).
"""

from aws_cdk import Stack, Tags
from constructs import Construct

from infra import config
from infra.database import EnvDatabase, MainDatabase, PreviewDatabase
from infra.environment import AppEnvironment
from infra.shared_refs import SharedRefs
from reconciler.registry import Registry
from reconciler.spec import EnvSpec


class EnvStack(Stack):
    def __init__(
        self, scope: Construct, id: str, *, spec: EnvSpec, registry: Registry, **kwargs
    ) -> None:
        config.validate_env_name(spec.env)
        super().__init__(
            scope,
            id,
            description=f"Preview-envs platform: environment {spec.env}",
            **kwargs,
        )
        is_main = spec.env == config.MAIN_ENV
        # Tags on every resource; `preview-env:kind` lets tooling tell previews from main (D21).
        Tags.of(self).add("preview-env:env", spec.env)
        Tags.of(self).add("preview-env:kind", "main" if is_main else "preview")

        shared = SharedRefs.from_ssm(self)
        database: EnvDatabase = (
            MainDatabase(self, "Database", shared=shared)
            if is_main
            else PreviewDatabase(self, "Database", shared=shared, env_name=spec.env)
        )
        self.environment_construct = AppEnvironment(
            self, "Env", spec=spec, registry=registry, shared=shared, database=database
        )
