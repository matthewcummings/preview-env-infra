"""Constants shared by the shared stack and the env stacks.

Anything both sides must agree on (SSM keys, AZ count, names) lives here, so the writer
(`preview-baseline`) and the readers (`preview-env-*`) can't drift apart.
"""

import re

# The shared baseline stack and the per-env stacks deliberately use different prefixes, so
# nothing that matches env stacks by prefix (the reconciler's env list, the CI role's
# DeleteStack permission) can ever match the shared stack.
SHARED_STACK_NAME = "preview-baseline"
ENV_STACK_PREFIX = "preview-env-"
MAIN_ENV = "main"

# Region used when neither the CDK CLI nor the environment says otherwise (D20).
DEFAULT_REGION = "us-east-1"

# The VPC spans exactly this many AZs. Env stacks import the subnet lists from SSM as
# comma-separated strings and split them with this assumed length, so the two stacks must
# agree on it (see shared_refs.py).
AZ_COUNT = 2

# Group names: strict, no normalization (contract "Naming"). The reconciler is the real
# gatekeeper; CDK re-checks because the name ends up in a stack name.
ENV_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")

# D42: the prefix list every env's ALB accepts HTTP from. Entries are managed by
# scripts/allow_ip.py, never by CloudFormation (see shared_stack.py).
ALB_ALLOWLIST_NAME = f"{SHARED_STACK_NAME}-alb-allowlist"
ALB_ALLOWLIST_MAX_ENTRIES = 20

# Default GitHub repo name of this (infra) repo; override with `-c infraRepo=<name>`.
DEFAULT_INFRA_REPO = "preview-env-infra"

# ECR lifecycle (D11). Tags are commit SHAs, so a SHA alone can't say "main" or "preview".
# Service CI adds a second tag `main-<sha>` to images built from main; see shared_stack.py.
ECR_MAIN_TAG_PREFIX = "main-"
ECR_KEEP_MAIN_IMAGES = 20
ECR_EXPIRE_OTHER_IMAGES_DAYS = 14


def env_stack_name(env: str) -> str:
    return f"{ENV_STACK_PREFIX}{env}"


def validate_env_name(env: str) -> None:
    if env != MAIN_ENV and not ENV_NAME_PATTERN.match(env):
        raise ValueError(
            f"invalid env name {env!r}: must be 'main' or match {ENV_NAME_PATTERN.pattern}"
        )


class SsmKeys:
    """SSM parameter names published by `preview-baseline` and read by `preview-env-*` (D24).

    SSM instead of CloudFormation exports: an export can't change while another stack
    imports it, which would freeze the shared stack; SSM parameters also resolve at deploy
    time, so `cdk synth` needs no AWS credentials.
    """

    _BASE = f"/{SHARED_STACK_NAME}"

    VPC_ID = f"{_BASE}/vpc-id"
    AVAILABILITY_ZONES = f"{_BASE}/availability-zones"  # comma-separated, AZ_COUNT items
    PUBLIC_SUBNET_IDS = f"{_BASE}/public-subnet-ids"  # comma-separated, AZ_COUNT items
    PRIVATE_SUBNET_IDS = f"{_BASE}/private-subnet-ids"  # comma-separated, AZ_COUNT items
    ECS_CLUSTER_NAME = f"{_BASE}/ecs-cluster-name"
    DB_CLUSTER_ARN = f"{_BASE}/db-cluster-arn"  # Data API target (preview DB create/drop)
    DB_ADMIN_SECRET_ARN = f"{_BASE}/db-admin-secret-arn"  # Data API credentials
    DB_ENDPOINT = f"{_BASE}/db-endpoint"
    DB_PORT = f"{_BASE}/db-port"
    DB_RESOURCE_ID = f"{_BASE}/db-resource-id"  # for rds-db:connect ARNs
    DB_CLIENT_SECURITY_GROUP_ID = f"{_BASE}/db-client-security-group-id"
    ALB_ALLOWLIST_PREFIX_LIST_ID = f"{_BASE}/alb-allowlist-prefix-list-id"  # D42
