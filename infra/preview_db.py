"""Preview databases (D40): one logical DB + DB user per service per preview env, on main's
Aurora cluster, created with the env stack and dropped with it.

Name = `<service db_name>__<group with '-' -> '_'>`, e.g. `service_a__cart_api`. Groups only
allow `[a-z0-9-]`, and a double underscore can't come from a group, so names can't collide
with each other or with main's `service_a` / `service_a_reader`.
"""

from infra import config
from infra.aurora_bootstrap import create_role_if_missing
from infra.data_api import SqlStep
from reconciler.registry import Service

POSTGRES_MAX_IDENTIFIER = 63

# Throttles so one preview can't starve dev or the other previews (D40).
PREVIEW_CONNECTION_LIMIT = 20
PREVIEW_STATEMENT_TIMEOUT = "30s"  # role default; copy-db and migrate lift it per session


def preview_db_name(service: Service, env: str) -> str:
    """Database name and DB user for `service` in preview env `env`."""
    config.validate_env_name(env)
    if env == config.MAIN_ENV:
        raise ValueError("main uses the service's own database, not a preview database")
    name = f"{service.db_name}__{env.replace('-', '_')}"
    if len(name) > POSTGRES_MAX_IDENTIFIER:
        raise ValueError(f"preview DB name {name!r} exceeds {POSTGRES_MAX_IDENTIFIER} chars")
    return name


def preview_db_steps(name: str) -> list[SqlStep]:
    """Create/drop SQL for one preview DB (contract revision "Env stack custom resources").

    CloudFormation deletes these in reverse order: the database is dropped (WITH FORCE, in
    case a connection lingers) before its role.
    """
    return [
        SqlStep(
            id="Role",
            sql=(
                "DO $$ BEGIN "
                + create_role_if_missing(name, f"LOGIN CONNECTION LIMIT {PREVIEW_CONNECTION_LIMIT}")
                + f" GRANT rds_iam TO {name};"
                f" ALTER ROLE {name} SET statement_timeout = '{PREVIEW_STATEMENT_TIMEOUT}';"
                # PG16+: needed for CREATE DATABASE ... OWNER and for DROP DATABASE.
                f" GRANT {name} TO CURRENT_USER;"
                " END $$;"
            ),
            on_delete_sql=f"DROP ROLE IF EXISTS {name};",
        ),
        SqlStep(
            id="Database",
            sql=f"CREATE DATABASE {name} OWNER {name};",
            rerun_on_update=False,
            on_delete_sql=f"DROP DATABASE IF EXISTS {name} WITH (FORCE);",
        ),
        SqlStep(
            id="Connect",
            sql=f"REVOKE CONNECT ON DATABASE {name} FROM PUBLIC;",
        ),
    ]
