import json

import pytest
from aws_cdk.assertions import Match, Template

from infra.aurora_bootstrap import bootstrap_steps
from infra.config import SsmKeys
from tests.infra.cdk_helpers import synth_shared


@pytest.fixture(scope="module")
def template(registry) -> Template:
    return synth_shared(registry)


def test_single_nat_gateway(template):
    template.resource_count_is("AWS::EC2::NatGateway", 1)


def test_vpc_has_public_private_and_isolated_subnets_in_two_azs(template):
    template.resource_count_is("AWS::EC2::Subnet", 6)


def test_aurora_serverless_v2_with_iam_auth_and_data_api(template):
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "Engine": "aurora-postgresql",
            "EngineVersion": "17.9",
            "EnableIAMDatabaseAuthentication": True,
            "EnableHttpEndpoint": True,
            "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 8},
        },
    )


def test_aurora_lives_in_isolated_subnets(template):
    subnet_group = next(iter(template.find_resources("AWS::RDS::DBSubnetGroup").values()))
    subnet_refs = json.dumps(subnet_group["Properties"]["SubnetIds"])
    assert "isolated" in subnet_refs and "private" not in subnet_refs.replace("isolated", "")


def test_one_ecr_repo_per_registered_service(template, registry):
    template.resource_count_is("AWS::ECR::Repository", len(registry.services))
    for svc in registry.services:
        template.has_resource_properties("AWS::ECR::Repository", {"RepositoryName": svc.name})


def test_ecr_lifecycle_protects_main_images_and_expires_the_rest(template):
    repo = next(iter(template.find_resources("AWS::ECR::Repository").values()))
    rules = json.loads(repo["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])["rules"]
    main_rule, rest_rule = sorted(rules, key=lambda r: r["rulePriority"])
    assert main_rule["selection"]["tagPatternList"] == ["main-*"]
    assert main_rule["selection"]["countType"] == "imageCountMoreThan"
    assert rest_rule["selection"]["tagStatus"] == "any"
    assert rest_rule["selection"]["countType"] == "sinceImagePushed"


def test_service_repo_roles_trust_only_their_own_repo(template, registry):
    for svc in registry.services:
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        Match.object_like(
                            {
                                "Action": "sts:AssumeRoleWithWebIdentity",
                                "Condition": {
                                    "StringEquals": {
                                        "token.actions.githubusercontent.com:aud": (
                                            "sts.amazonaws.com"
                                        )
                                    },
                                    "StringLike": {
                                        "token.actions.githubusercontent.com:sub": (
                                            f"repo:{registry.github_owner}/{svc.repo}:*"
                                        )
                                    },
                                },
                            }
                        )
                    ]
                }
            },
        )


def test_service_role_can_push_to_its_own_ecr_repo_only(template, registry):
    repos = template.find_resources("AWS::ECR::Repository")
    repo_id = {r["Properties"]["RepositoryName"]: logical for logical, r in repos.items()}
    roles = template.find_resources("AWS::IAM::Role")
    policies = template.find_resources("AWS::IAM::Policy")
    for svc in registry.services:
        role_id = next(k for k in roles if k.startswith(f"GithubOidc{svc.name.replace('-', '')}"))
        policy = next(p for p in policies.values() if {"Ref": role_id} in p["Properties"]["Roles"])
        text = json.dumps(policy["Properties"]["PolicyDocument"])
        assert "ecr:PutImage" in text
        assert repo_id[svc.name] in text
        others = [repo_id[o.name] for o in registry.services if o.name != svc.name]
        assert not any(other in text for other in others)


def test_infra_role_deploys_only_via_cdk_bootstrap_roles(template, registry):
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Condition": Match.object_like(
                                {
                                    "StringLike": {
                                        "token.actions.githubusercontent.com:sub": (
                                            f"repo:{registry.github_owner}/infra-repo:"
                                            "ref:refs/heads/main"
                                        )
                                    }
                                }
                            )
                        }
                    )
                ]
            }
        },
    )
    infra_policy = next(
        p
        for k, p in template.find_resources("AWS::IAM::Policy").items()
        if k.startswith("GithubOidcInfraRole")
    )
    actions = {
        a
        for stmt in infra_policy["Properties"]["PolicyDocument"]["Statement"]
        for a in ([stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"])
    }
    assert actions == {
        "sts:AssumeRole",
        "cloudformation:ListStacks",
        "cloudformation:DescribeStacks",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStackEvents",
        "cloudformation:ListStackResources",
        "ecr:DescribeImages",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ec2:ModifyManagedPrefixList",
        "ec2:GetManagedPrefixListEntries",
        "ec2:DescribeManagedPrefixLists",
    }
    # Writes to the allowlist are scoped to that one prefix list.
    (write,) = (
        stmt
        for stmt in infra_policy["Properties"]["PolicyDocument"]["Statement"]
        if "ec2:ModifyManagedPrefixList" in stmt["Action"]
    )
    assert write["Resource"] == {"Fn::GetAtt": ["AlbAllowlist", "Arn"]}


def test_shared_values_published_to_ssm_not_exports(template):
    for name in (v for k, v in vars(SsmKeys).items() if k.isupper() and not k.startswith("_")):
        template.has_resource_properties("AWS::SSM::Parameter", {"Name": name})
    for output in template.to_json().get("Outputs", {}).values():
        assert "Export" not in output


def test_aurora_bootstrap_runs_each_service_sql_via_data_api(template, registry):
    custom = template.find_resources("Custom::AWS")
    assert len(custom) == 3 * len(registry.services)
    text = json.dumps(custom)
    assert '\\"service\\":\\"rds-data\\"' in text
    for svc in registry.services:
        assert f"CREATE DATABASE {svc.db_name} OWNER {svc.db_name};" in text


def test_bootstrap_sql_matches_contract(registry):
    svc = registry.services[0]
    db, reader = svc.db_name, svc.db_reader
    roles, create_db, connect = (step.sql for step in bootstrap_steps(svc))
    assert f"CREATE ROLE {db} LOGIN;" in roles
    assert f"GRANT rds_iam TO {db};" in roles
    assert f"CREATE ROLE {reader} LOGIN;" in roles
    assert f"GRANT rds_iam TO {reader};" in roles
    assert f"GRANT pg_read_all_data TO {reader};" in roles
    # CREATE DATABASE can't run in a transaction/DO block: it must be a statement of its own.
    assert create_db == f"CREATE DATABASE {db} OWNER {db};"
    assert f"REVOKE CONNECT ON DATABASE {db} FROM PUBLIC;" in connect
    assert f"GRANT CONNECT ON DATABASE {db} TO {reader};" in connect


def test_registry_drives_repos_and_roles(three_service_registry):
    template = synth_shared(three_service_registry)
    template.resource_count_is("AWS::ECR::Repository", 3)
    template.resource_count_is("Custom::AWS", 9)
    github_roles = template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Statement": [Match.object_like({"Action": "sts:AssumeRoleWithWebIdentity"})]
                }
            }
        },
    )
    assert len(github_roles) == 3 + 1  # one per service repo + the infra repo


def test_oidc_provider_created_by_default(template):
    template.resource_count_is("AWS::IAM::OIDCProvider", 1)


def test_oidc_provider_can_reuse_the_existing_one(registry):
    """D39: `-c githubOidcProvider=existing` imports the provider by its fixed ARN."""
    template = synth_shared(registry, create_oidc_provider=False)
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)
    roles = template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Statement": [Match.object_like({"Action": "sts:AssumeRoleWithWebIdentity"})]
                }
            }
        },
    )
    assert len(roles) == len(registry.services) + 1
    for role in roles.values():
        federated = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"][
            "Federated"
        ]
        assert json.dumps(federated).endswith(
            ':oidc-provider/token.actions.githubusercontent.com"]]}'
        )


def test_publishes_data_api_targets_for_preview_databases(template):
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": SsmKeys.DB_CLUSTER_ARN, "Value": Match.any_value()},
    )
    admin_secret = template.find_resources(
        "AWS::SSM::Parameter", {"Properties": {"Name": SsmKeys.DB_ADMIN_SECRET_ARN}}
    )
    (param,) = admin_secret.values()
    assert "AuroraSecret" in json.dumps(param["Properties"]["Value"])


def test_alb_allowlist_prefix_list_has_no_entries(template):
    """D42: entries live outside CloudFormation (scripts/allow_ip.py), so none in the template."""
    (prefix_list,) = template.find_resources("AWS::EC2::PrefixList").values()
    assert prefix_list["Properties"] == {
        "AddressFamily": "IPv4",
        "MaxEntries": 20,
        "PrefixListName": "preview-baseline-alb-allowlist",
    }
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID,
            "Value": {"Fn::GetAtt": ["AlbAllowlist", "PrefixListId"]},
        },
    )
