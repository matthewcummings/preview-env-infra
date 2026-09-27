"""A built-in EnvSpec so `cdk synth` works offline, without a reconciler run (contract "CDK app").

For synth and reviewing templates only: the image tags don't exist, so deploying it would
fail the circuit breaker. Real deploys always pass `-c envSpec=<file>` from the reconciler.
"""

from aws_cdk import Aws

from reconciler.registry import Registry
from reconciler.spec import EnvSpec, ServiceSpec

SAMPLE_ENV = "sample"
SAMPLE_SHA = "0" * 40


def sample_env_spec(registry: Registry, env: str = SAMPLE_ENV) -> EnvSpec:
    # Account and region stay CloudFormation pseudo-parameters: nothing account-specific
    # in code (D2/D20).
    registry_host = f"{Aws.ACCOUNT_ID}.dkr.ecr.{Aws.REGION}.{Aws.URL_SUFFIX}"
    return EnvSpec(
        env=env,
        services={
            svc.name: ServiceSpec(
                ref="main", sha=SAMPLE_SHA, image=f"{registry_host}/{svc.name}:{SAMPLE_SHA}"
            )
            for svc in registry.services
        },
    )
