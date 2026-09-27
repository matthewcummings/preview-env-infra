"""Aurora bootstrap in `pe-shared`: main's per-service roles and databases (Data API).

Per service (contract "Aurora bootstrap"; D12, D16, D18):
- `service_x`: LOGIN, rds_iam (IAM auth only, no password), owns database `service_x`.
- `service_x_reader`: LOGIN, rds_iam, CONNECT on `service_x`, pg_read_all_data. Previews
  copy main's data as this user, so they can read main but never write to it.
- `REVOKE CONNECT ... FROM PUBLIC`: other services' users can't even connect.

Preview databases are created by the env stacks themselves (see preview_db.py).
"""

from aws_cdk import aws_rds as rds
from constructs import Construct

from infra.data_api import DataApiSql, SqlStep
from reconciler.registry import Service


def create_role_if_missing(role: str, options: str = "LOGIN") -> str:
    """PL/pgSQL fragment (for a DO block): CREATE ROLE, skipped if it already exists."""
    return (
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {role} {options}; END IF;"
    )


def bootstrap_steps(service: Service) -> list[SqlStep]:
    """The SQL for one service. Role and grant steps are DO blocks with IF NOT EXISTS, so
    they are safe to rerun (GRANT/REVOKE are idempotent already)."""
    db = service.db_name
    reader = service.db_reader
    return [
        SqlStep(
            id="Roles",
            sql=(
                "DO $$ BEGIN "
                f"{create_role_if_missing(db)} "
                f"GRANT rds_iam TO {db}; "
                # PG16+: CREATE DATABASE ... OWNER x requires the admin to be able to
                # SET ROLE x. The admin gets ADMIN OPTION on roles it creates, but not SET,
                # so grant it membership explicitly.
                f"GRANT {db} TO CURRENT_USER; "
                f"{create_role_if_missing(reader)} "
                f"GRANT rds_iam TO {reader}; "
                f"GRANT pg_read_all_data TO {reader}; "
                "END $$;"
            ),
        ),
        SqlStep(
            id="Database",
            sql=f"CREATE DATABASE {db} OWNER {db};",
            rerun_on_update=False,
        ),
        SqlStep(
            id="Connect",
            sql=(
                "DO $$ BEGIN "
                f"REVOKE CONNECT ON DATABASE {db} FROM PUBLIC; "
                f"GRANT CONNECT ON DATABASE {db} TO {reader}; "
                "END $$;"
            ),
        ),
        # No delete SQL anywhere: removing a service from the registry (or deleting
        # pe-shared) must not drop main's data. Drop it by hand if that's really intended.
    ]


class AuroraBootstrap(Construct):
    """Creates each registered service's roles and database on the shared Aurora cluster."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cluster: rds.DatabaseCluster,
        services: tuple[Service, ...],
    ) -> None:
        super().__init__(scope, id)
        assert cluster.secret is not None, "bootstrap needs the cluster's generated admin secret"

        for service in services:
            sql = DataApiSql(
                self,
                service.name,
                cluster_arn=cluster.cluster_arn,
                admin_secret_arn=cluster.secret.secret_arn,
                steps=bootstrap_steps(service),
                physical_id_prefix=service.db_name,
            )
            # The writer instance (a child of the cluster) must be up before any SQL.
            sql.resources[0].node.add_dependency(cluster)
