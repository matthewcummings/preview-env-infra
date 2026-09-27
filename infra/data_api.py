"""Run SQL on the shared Aurora cluster from CloudFormation, via the RDS Data API.

Each step is a CDK `AwsCustomResource` calling `rds-data:ExecuteStatement` with the admin
secret. No Lambda code of our own and no VPC networking: the Data API is an HTTPS endpoint,
so this works even though Aurora sits in isolated subnets.

Used by `pe-shared` (main's per-service roles and databases) and by every preview env stack
(the env's own databases, created with the stack and dropped with it; D40).

Each Data API call runs one statement in autocommit mode. `CREATE DATABASE` and
`DROP DATABASE` can't run inside a transaction block (or a DO block), so they are always
steps of their own.
"""

from dataclasses import dataclass

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

# The Data API needs a database to connect to; `postgres` always exists.
ADMIN_DATABASE = "postgres"
_LOG_GROUP_ID = "DataApiSqlLogs"


def _provider_log_group(scope: Construct) -> logs.ILogGroup:
    """One log group per stack for the (singleton) custom resource Lambda, deleted with the
    stack. CDK's default would be retained, leaving one orphaned log group per preview (D14).
    """
    stack = Stack.of(scope)
    existing = stack.node.try_find_child(_LOG_GROUP_ID)
    if existing is not None:
        return existing  # type: ignore[return-value]
    return logs.LogGroup(
        stack,
        _LOG_GROUP_ID,
        retention=logs.RetentionDays.TWO_WEEKS,
        removal_policy=RemovalPolicy.DESTROY,
    )


@dataclass(frozen=True)
class SqlStep:
    id: str
    sql: str
    # Rerun `sql` when it changes on a stack update. False for CREATE DATABASE, which has
    # no IF NOT EXISTS and would fail on a rerun.
    rerun_on_update: bool = True
    # Runs when the resource is deleted (stack deletion, or the step being removed).
    # CloudFormation deletes in reverse dependency order, so the steps' delete SQL runs
    # last-step-first: e.g. DROP DATABASE before DROP ROLE.
    on_delete_sql: str | None = None


class DataApiSql(Construct):
    """Runs `steps` in order, one custom resource per step, each depending on the previous."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster_arn: str,
        admin_secret_arn: str,
        steps: list[SqlStep],
        physical_id_prefix: str,
    ) -> None:
        super().__init__(scope, id)
        policy = cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(actions=["rds-data:ExecuteStatement"], resources=[cluster_arn]),
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[admin_secret_arn]
                ),
            ]
        )

        def call(step_id: str, sql: str) -> cr.AwsSdkCall:
            return cr.AwsSdkCall(
                service="rds-data",
                action="ExecuteStatement",
                parameters={
                    "resourceArn": cluster_arn,
                    "secretArn": admin_secret_arn,
                    "database": ADMIN_DATABASE,
                    "sql": sql,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{physical_id_prefix}-{step_id.lower()}"
                ),
            )

        self.resources: list[cr.AwsCustomResource] = []
        for step in steps:
            create = call(step.id, step.sql)
            resource = cr.AwsCustomResource(
                self,
                step.id,
                on_create=create,
                on_update=create if step.rerun_on_update else None,
                on_delete=call(step.id, step.on_delete_sql) if step.on_delete_sql else None,
                policy=policy,
                install_latest_aws_sdk=False,
                log_group=_provider_log_group(self),
                timeout=Duration.minutes(2),
            )
            if self.resources:
                resource.node.add_dependency(self.resources[-1])
            self.resources.append(resource)

    @property
    def last(self) -> cr.AwsCustomResource:
        """Depend on this to run after every step (and be deleted before any of them)."""
        return self.resources[-1]
