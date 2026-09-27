"""The pluggable database component (D4, D40): the only place that knows main and previews
get their databases differently.

The environment construct hands each service's task definition to an `EnvDatabase`, which
adds the containers that must run before the app, grants the task role its DB access, and
returns the env vars the app needs. Both implementations point the app at main's Aurora
cluster with IAM auth and produce the same variables (`DB_HOST`, `DB_NAME`, `DB_USER`,
`DB_AUTH`, ...), so the app and the rest of the environment are identical by construction.

- `MainDatabase` (env `main`): the service's own database `service_x`, as user `service_x`.
  Task: migrate (SUCCESS) -> seed (SUCCESS) -> app.
- `PreviewDatabase` (previews): a database of the env's own, `service_x__<group>`, created
  and dropped with the env stack; filled once from main via the read-only `service_x_reader`.
  Task: copy-db (SUCCESS) -> migrate (SUCCESS) -> app. No seed: the data comes from main.
"""

from dataclasses import dataclass, field

from aws_cdk import Aws
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from constructs import Construct, IDependable

from infra.data_api import DataApiSql
from infra.preview_db import preview_db_name, preview_db_steps
from infra.shared_refs import SharedRefs
from reconciler.registry import Service

# Previews get small connection pools: preview traffic is tiny, and every preview shares
# main's cluster, so this halves connection pressure (D41). Main keeps the app defaults (5+5).
PREVIEW_POOL_ENV = {"DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "3"}


@dataclass(frozen=True)
class ServiceTask:
    """What the environment gives the database component for one service."""

    service: Service
    task_definition: ecs.FargateTaskDefinition
    image: ecs.ContainerImage  # the service's own image (it also carries migrate/seed/copy-db)
    base_environment: dict[str, str]  # SERVICE_NAME, ENV_NAME, PATH_PREFIX, PORT
    logging: ecs.LogDriver


@dataclass(frozen=True)
class DatabaseWiring:
    """What the database component gives back for the app container and the service."""

    environment: dict[str, str]
    # The app container starts only after these (the last helper container succeeded).
    app_depends_on: list[ecs.ContainerDependency] = field(default_factory=list)
    security_groups: list[ec2.ISecurityGroup] = field(default_factory=list)
    # The ECS service depends on these, so on stack deletion its tasks stop first and the
    # database is dropped after (D40).
    service_depends_on: list[IDependable] = field(default_factory=list)


def app_command(command: str) -> list[str]:
    """One image, several commands (contract "App container").

    The image's ENTRYPOINT is `python -m app` and its default CMD is `serve`, so a
    container only overrides the subcommand. The app container sets nothing (-> serve).
    """
    return [command]


class EnvDatabase(Construct):
    """Base class: the interface plus helpers shared by both modes.

    (Not an `abc.ABC`: jsii constructs have their own metaclass.)
    """

    def __init__(self, scope: Construct, id: str, *, shared: SharedRefs) -> None:
        super().__init__(scope, id)
        self.shared = shared

    def wire(self, task: ServiceTask) -> DatabaseWiring:
        """Add this mode's containers and grants to `task`; return the app's DB settings."""
        raise NotImplementedError

    def _aurora_env(self, db_name: str, user: str, *, prefix: str = "") -> dict[str, str]:
        """Connection settings for main's Aurora cluster with IAM auth (D16)."""
        return {
            f"{prefix}DB_HOST": self.shared.db_endpoint,
            f"{prefix}DB_PORT": self.shared.db_port,
            f"{prefix}DB_NAME": db_name,
            f"{prefix}DB_USER": user,
            f"{prefix}DB_AUTH": "iam",
            f"{prefix}DB_SSLMODE": "require",  # IAM auth requires TLS
        }

    def _grant_rds_connect(self, task: ServiceTask, *users: str) -> None:
        """Allow this task to get IAM auth tokens for exactly these DB users (D16)."""
        task.task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["rds-db:connect"],
                resources=[
                    f"arn:{Aws.PARTITION}:rds-db:{Aws.REGION}:{Aws.ACCOUNT_ID}:dbuser:"
                    f"{self.shared.db_resource_id}/{user}"
                    for user in users
                ],
            )
        )

    @staticmethod
    def _add_job(
        task: ServiceTask,
        name: str,
        *,
        environment: dict[str, str],
        after: ecs.ContainerDefinition | None = None,
    ) -> ecs.ContainerDefinition:
        """A run-to-completion container (`python -m app <name>`) that gates the next one.

        Not essential: it's expected to exit. If it exits non-zero, the SUCCESS dependency
        is never met, the app never starts, and the deployment circuit breaker rolls back.
        """
        container = task.task_definition.add_container(
            name,
            image=task.image,
            command=app_command(name),
            essential=False,
            environment={**task.base_environment, **environment},
            logging=task.logging,
        )
        if after is not None:
            container.add_container_dependencies(*EnvDatabase._success(after))
        return container

    @staticmethod
    def _success(container: ecs.ContainerDefinition) -> list[ecs.ContainerDependency]:
        return [
            ecs.ContainerDependency(
                container=container, condition=ecs.ContainerDependencyCondition.SUCCESS
            )
        ]


class MainDatabase(EnvDatabase):
    """main: the service's own database on the shared cluster, as the service's user."""

    def wire(self, task: ServiceTask) -> DatabaseWiring:
        service = task.service
        db_env = {
            **self._aurora_env(service.db_name, service.db_name),
            "AWS_REGION": Aws.REGION,
        }
        self._grant_rds_connect(task, service.db_name)

        migrate = self._add_job(task, "migrate", environment=db_env)
        seed = self._add_job(task, "seed", environment=db_env, after=migrate)
        return DatabaseWiring(
            environment=db_env,
            app_depends_on=self._success(seed),
            security_groups=[self.shared.db_client_sg],
        )


class PreviewDatabase(EnvDatabase):
    """Previews: a database per service for this env, on the same cluster (D40)."""

    def __init__(self, scope: Construct, id: str, *, shared: SharedRefs, env_name: str) -> None:
        super().__init__(scope, id, shared=shared)
        self.env_name = env_name

    def wire(self, task: ServiceTask) -> DatabaseWiring:
        service = task.service
        name = preview_db_name(service, self.env_name)

        # Created with the env stack, dropped with it (delete SQL on the custom resources).
        sql = DataApiSql(
            self,
            service.name,
            cluster_arn=self.shared.db_cluster_arn,
            admin_secret_arn=self.shared.db_admin_secret_arn,
            steps=preview_db_steps(name),
            physical_id_prefix=name,
        )

        db_env = {
            **self._aurora_env(name, name),
            **PREVIEW_POOL_ENV,
            "AWS_REGION": Aws.REGION,
        }
        # Source = main's database, as the read-only reader user: previews can read main
        # but never write to it (D18). copy-db copies only while the target is empty.
        copy_env = {
            **db_env,
            **self._aurora_env(service.db_name, service.db_reader, prefix="SOURCE_"),
        }
        # Exactly two users: this env's own, and main's reader.
        self._grant_rds_connect(task, name, service.db_reader)

        copy_db = self._add_job(task, "copy-db", environment=copy_env)
        migrate = self._add_job(task, "migrate", environment=db_env, after=copy_db)
        return DatabaseWiring(
            environment=db_env,
            app_depends_on=self._success(migrate),
            security_groups=[self.shared.db_client_sg],
            service_depends_on=[sql.last],
        )
