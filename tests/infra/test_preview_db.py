import pytest

from infra.preview_db import preview_db_name, preview_db_steps


def test_preview_db_name_joins_service_db_and_group(registry):
    svc = registry.service("service-a")
    assert preview_db_name(svc, "cart-api") == "service_a__cart_api"
    assert preview_db_name(svc, "checkout") == "service_a__checkout"


@pytest.mark.parametrize("env", ["main", "Bad_Name", "x" * 21])
def test_preview_db_name_rejects_main_and_invalid_groups(registry, env):
    with pytest.raises(ValueError):
        preview_db_name(registry.service("service-a"), env)


def test_preview_db_name_fits_postgres_identifier_limit(registry):
    assert len(preview_db_name(registry.service("service-a"), "a" * 20)) <= 63


def test_preview_db_sql_matches_contract():
    role, database, connect = preview_db_steps("service_a__checkout")
    u = "service_a__checkout"
    assert f"CREATE ROLE {u} LOGIN CONNECTION LIMIT 20;" in role.sql
    assert f"GRANT rds_iam TO {u};" in role.sql
    assert f"ALTER ROLE {u} SET statement_timeout = '30s';" in role.sql
    assert f"GRANT {u} TO CURRENT_USER;" in role.sql
    # CREATE/DROP DATABASE can't run in a transaction or DO block: statements of their own.
    assert database.sql == f"CREATE DATABASE {u} OWNER {u};"
    assert database.on_delete_sql == f"DROP DATABASE IF EXISTS {u} WITH (FORCE);"
    assert database.rerun_on_update is False
    assert connect.sql == f"REVOKE CONNECT ON DATABASE {u} FROM PUBLIC;"
    assert role.on_delete_sql == f"DROP ROLE IF EXISTS {u};"
    assert connect.on_delete_sql is None
