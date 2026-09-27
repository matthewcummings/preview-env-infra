import json

import pytest
from aws_cdk.assertions import Template

from infra.config import SsmKeys
from infra.env_stack import EnvStack
from infra.environment import image_tag_or_digest
from reconciler.spec import EnvSpec
from tests.infra.cdk_helpers import make_spec, new_app, synth_env


@pytest.fixture(scope="module")
def main_template(registry) -> Template:
    return synth_env(registry, make_spec(registry, "main"))


@pytest.fixture(scope="module")
def preview_template(registry) -> Template:
    return synth_env(registry, make_spec(registry, "checkout"))


@pytest.fixture(params=["main", "preview"])
def any_template(request, main_template, preview_template) -> Template:
    return main_template if request.param == "main" else preview_template


def containers(template: Template, service_name: str) -> dict[str, dict]:
    """Container definitions of one service's task, by container name."""
    prefix = f"Env{service_name.replace('-', '')}Task"
    task = next(
        r
        for k, r in template.find_resources("AWS::ECS::TaskDefinition").items()
        if k.startswith(prefix)
    )
    return {c["Name"]: c for c in task["Properties"]["ContainerDefinitions"]}


def depends_on(container: dict) -> list[tuple[str, str]]:
    return [(d["ContainerName"], d["Condition"]) for d in container.get("DependsOn", [])]


def env_vars(container: dict) -> dict:
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


def rds_connect_users(template: Template, service_name: str) -> list[str]:
    """DB users in the rds-db:connect ARNs granted to one service's task role."""
    prefix = f"Env{service_name.replace('-', '')}TaskTaskRoleDefaultPolicy"
    policy = next(
        p for k, p in template.find_resources("AWS::IAM::Policy").items() if k.startswith(prefix)
    )
    users = []
    for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
        if stmt["Action"] == "rds-db:connect":
            resources = stmt["Resource"]
            for arn in resources if isinstance(resources, list) else [resources]:
                users.append(arn["Fn::Join"][1][-1].rsplit("/", 1)[-1])
    return sorted(users)


def custom_resources(template: Template, service_name: str) -> dict[str, dict]:
    """Data API custom resources of one service's preview DB, by step (Role/Database/...)."""
    prefix = f"Database{service_name.replace('-', '')}"
    return {
        step: r
        for k, r in template.find_resources("Custom::AWS").items()
        for step in ("Role", "Database", "Connect")
        if k.startswith(prefix + step)
    }


def sql_of(resource: dict, event: str) -> str:
    """The SQL a custom resource runs on Create/Delete (inside its Fn::Join'd JSON)."""
    body = resource["Properties"].get(event)
    if body is None:
        return ""
    text = "".join(p for p in body["Fn::Join"][1] if isinstance(p, str))
    return text.split('"sql":"', 1)[1].split('"}', 1)[0]


# --- Shape shared by both database modes -------------------------------------------------


def test_one_alb_per_env_with_a_rule_per_service(any_template, registry):
    any_template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)
    any_template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internet-facing"}
    )
    any_template.resource_count_is(
        "AWS::ElasticLoadBalancingV2::ListenerRule", len(registry.services)
    )
    for svc in registry.services:
        any_template.has_resource_properties(
            "AWS::ElasticLoadBalancingV2::ListenerRule",
            {
                "Conditions": [
                    {
                        "Field": "path-pattern",
                        "PathPatternConfig": {"Values": [svc.path_prefix, f"{svc.path_prefix}/*"]},
                    }
                ]
            },
        )
        any_template.has_resource_properties(
            "AWS::ElasticLoadBalancingV2::TargetGroup",
            {"HealthCheckPath": f"{svc.path_prefix}{svc.health_path}", "TargetType": "ip"},
        )


def test_fargate_arm64_with_circuit_breaker_rollback(any_template, registry):
    any_template.resource_count_is("AWS::ECS::Service", len(registry.services))
    any_template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "LaunchType": "FARGATE",
            "DeploymentConfiguration": {
                "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}
            },
            "HealthCheckGracePeriodSeconds": 180,
            "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "DISABLED"}},
        },
    )
    any_template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"}},
    )


def _ingress_into(template: Template, sg_prefix: str) -> list[dict]:
    return [
        r["Properties"]
        for r in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"]["GroupId"]["Fn::GetAtt"][0].startswith(sg_prefix)
    ]


def test_task_security_group_only_allows_the_alb(any_template, registry):
    for svc in registry.services:
        (rule,) = _ingress_into(any_template, f"Env{svc.name.replace('-', '')}TaskSg")
        assert rule["SourceSecurityGroupId"]["Fn::GetAtt"][0].startswith("EnvAlbSg")
        assert rule["FromPort"] == rule["ToPort"] == svc.port


def _ssm_param_id(key: str) -> str:
    """The logical-ID prefix CDK gives a deploy-time SSM lookup: the path, alphanumerics only.

    Derived from the key, so renaming the SSM paths can't leave these tests stale.
    """
    return "SsmParameterValue" + "".join(c for c in key if c.isalnum())


def test_alb_accepts_http_only_from_the_allowlist_prefix_list(any_template):
    """D42: no 0.0.0.0/0 anywhere; port 80 only from the SSM-provided prefix list."""
    (rule,) = _ingress_into(any_template, "EnvAlbSg")
    assert rule["FromPort"] == rule["ToPort"] == 80
    assert rule["SourcePrefixListId"]["Ref"].startswith(
        _ssm_param_id(SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID)
    )
    raw = any_template.to_json()
    params = raw["Parameters"]
    assert params[rule["SourcePrefixListId"]["Ref"]]["Default"] == (
        "/preview-baseline/alb-allowlist-prefix-list-id"
    )
    for sg in any_template.find_resources("AWS::EC2::SecurityGroup").values():
        for inbound in sg["Properties"].get("SecurityGroupIngress", []):
            assert inbound.get("CidrIp") != "0.0.0.0/0"
            assert inbound.get("CidrIpv6") != "::/0"
    for inbound in any_template.find_resources("AWS::EC2::SecurityGroupIngress").values():
        assert "CidrIp" not in inbound["Properties"]
        assert "CidrIpv6" not in inbound["Properties"]


def test_log_groups_are_removed_with_the_stack(any_template):
    for log_group in any_template.find_resources("AWS::Logs::LogGroup").values():
        assert log_group["DeletionPolicy"] == "Delete"


def test_shared_values_come_from_ssm_not_exports(any_template):
    raw = any_template.to_json()
    assert "Fn::ImportValue" not in json.dumps(raw)
    for output in raw.get("Outputs", {}).values():
        assert "Export" not in output
    ssm_params = [
        p["Default"]
        for p in raw["Parameters"].values()
        if p["Type"] == "AWS::SSM::Parameter::Value<String>"
    ]
    assert "/preview-baseline/vpc-id" in ssm_params
    assert "/preview-baseline/db-endpoint" in ssm_params


def test_outputs_the_env_url(any_template):
    urls = [o for k, o in any_template.to_json()["Outputs"].items() if k.startswith("EnvUrl")]
    assert len(urls) == 1
    assert urls[0]["Value"]["Fn::Join"][1][0] == "http://"


def test_app_env_vars_and_image(any_template, registry):
    for svc in registry.services:
        app = containers(any_template, svc.name)["app"]
        # Pinned by digest to the service's own ECR repo in this account/region.
        assert app["Image"]["Fn::Join"][1][-1] == f"/{svc.name}@sha256:{'b' * 64}"
        assert "Command" not in app  # image default CMD: serve
        env = env_vars(app)
        assert env["SERVICE_NAME"] == svc.name
        assert env["PATH_PREFIX"] == svc.path_prefix
        assert env["PORT"] == str(svc.port)


def test_every_env_uses_aurora_with_iam_auth(any_template, registry):
    for svc in registry.services:
        for container in containers(any_template, svc.name).values():
            env = env_vars(container)
            assert env["DB_AUTH"] == "iam"
            assert env["DB_SSLMODE"] == "require"
            assert "Ref" in env["DB_HOST"]  # the SSM-backed Aurora endpoint
            assert "Secrets" not in container  # IAM auth: no DB passwords anywhere


def test_no_sidecar_anywhere(any_template, registry):
    any_template.resource_count_is("AWS::SecretsManager::Secret", 0)
    for svc in registry.services:
        for container in containers(any_template, svc.name).values():
            assert "HealthCheck" not in container
            assert "postgres" not in json.dumps(container["Image"])


# --- main: its own databases -----------------------------------------------------------------


def test_main_container_order_is_migrate_seed_app(main_template, registry):
    for svc in registry.services:
        c = containers(main_template, svc.name)
        assert set(c) == {"migrate", "seed", "app"}
        assert depends_on(c["migrate"]) == []
        assert depends_on(c["seed"]) == [("migrate", "SUCCESS")]
        assert depends_on(c["app"]) == [("seed", "SUCCESS")]
        assert c["migrate"]["Essential"] is False and c["seed"]["Essential"] is False
        # Only the subcommand: the image's ENTRYPOINT is `python -m app`.
        assert c["migrate"]["Command"] == ["migrate"]
        assert c["seed"]["Command"] == ["seed"]


def test_main_uses_the_services_own_database(main_template, registry):
    for svc in registry.services:
        env = env_vars(containers(main_template, svc.name)["app"])
        assert env["DB_NAME"] == svc.db_name
        assert env["DB_USER"] == svc.db_name
        # Main keeps the app's default pool sizes.
        assert "DB_POOL_SIZE" not in env and "DB_MAX_OVERFLOW" not in env


def test_main_task_role_connects_as_its_own_user_only(main_template, registry):
    for svc in registry.services:
        assert rds_connect_users(main_template, svc.name) == [svc.db_name]


def test_main_creates_no_databases(main_template):
    # main's databases come from the preview-baseline bootstrap and are never dropped by an env.
    main_template.resource_count_is("Custom::AWS", 0)


# --- previews: a database per service per env on the same cluster (D40) -----------------------


def test_preview_container_order_is_copy_migrate_app(preview_template, registry):
    for svc in registry.services:
        c = containers(preview_template, svc.name)
        assert set(c) == {"copy-db", "migrate", "app"}  # no seed: the data comes from main
        assert depends_on(c["copy-db"]) == []
        assert depends_on(c["migrate"]) == [("copy-db", "SUCCESS")]
        assert depends_on(c["app"]) == [("migrate", "SUCCESS")]
        assert c["copy-db"]["Command"] == ["copy-db"]
        assert c["migrate"]["Command"] == ["migrate"]


def test_preview_uses_its_own_database_and_copies_from_main_as_reader(preview_template, registry):
    for svc in registry.services:
        own = f"{svc.db_name}__checkout"
        c = containers(preview_template, svc.name)
        for name in ("copy-db", "migrate", "app"):
            env = env_vars(c[name])
            assert env["DB_NAME"] == own
            assert env["DB_USER"] == own
            assert env["DB_POOL_SIZE"] == "2"
            assert env["DB_MAX_OVERFLOW"] == "3"
        copy_env = env_vars(c["copy-db"])
        assert copy_env["SOURCE_DB_NAME"] == svc.db_name
        assert copy_env["SOURCE_DB_USER"] == svc.db_reader
        assert copy_env["SOURCE_DB_AUTH"] == "iam"
        assert copy_env["SOURCE_DB_SSLMODE"] == "require"
        assert "SOURCE_DB_USER" not in env_vars(c["app"])


def test_preview_task_role_connects_as_own_user_and_main_reader_only(preview_template, registry):
    for svc in registry.services:
        assert rds_connect_users(preview_template, svc.name) == sorted(
            [f"{svc.db_name}__checkout", svc.db_reader]
        )


def test_preview_database_created_and_dropped_via_data_api(preview_template, registry):
    preview_template.resource_count_is("Custom::AWS", 3 * len(registry.services))
    for svc in registry.services:
        db = f"{svc.db_name}__checkout"
        cr = custom_resources(preview_template, svc.name)
        assert "rds-data" in json.dumps(cr["Role"]["Properties"]["Create"])
        assert f"CREATE ROLE {db} LOGIN CONNECTION LIMIT 20;" in sql_of(cr["Role"], "Create")
        assert sql_of(cr["Database"], "Create") == f"CREATE DATABASE {db} OWNER {db};"
        assert sql_of(cr["Connect"], "Create") == f"REVOKE CONNECT ON DATABASE {db} FROM PUBLIC;"
        assert sql_of(cr["Database"], "Delete") == f"DROP DATABASE IF EXISTS {db} WITH (FORCE);"
        assert sql_of(cr["Role"], "Delete") == f"DROP ROLE IF EXISTS {db};"
        assert sql_of(cr["Connect"], "Delete") == ""


def test_preview_delete_order_is_service_then_database_then_role(preview_template, registry):
    """CloudFormation deletes dependents first: ECS service -> Connect -> Database -> Role."""
    services = preview_template.find_resources("AWS::ECS::Service")
    for svc in registry.services:
        cr_ids = {
            step: k
            for k in preview_template.find_resources("Custom::AWS")
            for step in ("Role", "Database", "Connect")
            if k.startswith(f"Database{svc.name.replace('-', '')}{step}")
        }
        resources = preview_template.to_json()["Resources"]
        assert cr_ids["Database"] in resources[cr_ids["Connect"]]["DependsOn"]
        assert cr_ids["Role"] in resources[cr_ids["Database"]]["DependsOn"]
        ecs_service = next(
            v for k, v in services.items() if k.startswith(f"Env{svc.name.replace('-', '')}")
        )
        assert cr_ids["Connect"] in ecs_service["DependsOn"]


def test_preview_data_api_role_is_scoped_to_cluster_and_admin_secret(preview_template):
    text = json.dumps(preview_template.find_resources("AWS::IAM::Policy"))
    assert "rds-data:ExecuteStatement" in text
    assert _ssm_param_id(SsmKeys.DB_CLUSTER_ARN) in text
    assert "secretsmanager:GetSecretValue" in text
    assert _ssm_param_id(SsmKeys.DB_ADMIN_SECRET_ARN) in text


def test_preview_runs_one_task_per_service(preview_template):
    for service in preview_template.find_resources("AWS::ECS::Service").values():
        assert service["Properties"]["DesiredCount"] == 1


# --- input validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (f"1.dkr.ecr.us-east-1.amazonaws.com/service-a@sha256:{'e' * 64}", f"sha256:{'e' * 64}"),
        ("1.dkr.ecr.us-east-1.amazonaws.com/service-a:" + "f" * 40, "f" * 40),
        ("service-a:abc", "abc"),
    ],
)
def test_image_tag_or_digest(registry, image, expected):
    assert image_tag_or_digest(registry.service("service-a"), image) == expected


@pytest.mark.parametrize(
    "image",
    [
        f"1.dkr.ecr.us-east-1.amazonaws.com/service-b@sha256:{'e' * 64}",  # another service's repo
        "docker.io/library/nginx:latest",
        "service-a",  # no tag or digest
        "service-a@sha256:short",
    ],
)
def test_image_must_come_from_the_services_own_repo(registry, image):
    with pytest.raises(ValueError, match="must be"):
        image_tag_or_digest(registry.service("service-a"), image)


def test_spec_must_cover_every_registered_service(registry):
    spec = make_spec(registry, "checkout")
    partial = EnvSpec(env="checkout", services=dict(list(spec.services.items())[:1]))
    with pytest.raises(ValueError, match="doesn't match the registry"):
        EnvStack(new_app(), "preview-env-checkout", spec=partial, registry=registry)


def test_invalid_env_name_is_rejected(registry):
    with pytest.raises(ValueError, match="invalid env name"):
        EnvStack(new_app(), "x", spec=make_spec(registry, "Bad_Name"), registry=registry)


def test_registry_drives_services(three_service_registry):
    template = synth_env(three_service_registry, make_spec(three_service_registry, "demo"))
    template.resource_count_is("AWS::ECS::Service", 3)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::ListenerRule", 3)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)
