"""GitHub Actions -> AWS via OIDC: no long-lived AWS keys anywhere (D11, D32).

- One role per service repo: may push images to its own ECR repo and nothing else.
- One role for the infra repo: deploys only through CDK (by assuming the CDK bootstrap
  roles, so every change goes through CloudFormation), plus the few direct reads the
  reconciler needs.
"""

from aws_cdk import Aws, CfnOutput
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.config import PREFIX
from reconciler.registry import Registry

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
STS_AUDIENCE = "sts.amazonaws.com"


class GithubOidc(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        registry: Registry,
        repositories: dict[str, ecr.IRepository],
        infra_repo: str,
        create_provider: bool = True,
    ) -> None:
        super().__init__(scope, id)
        owner = registry.github_owner

        # An account can have only one provider per URL (D39). Default: create it
        # (AWS::IAM::OIDCProvider, no custom resource). If the account already has one, the
        # deploy script passes `-c githubOidcProvider=existing` and we import it by its
        # fixed ARN: no lookup, so `cdk synth` still needs no credentials.
        self.provider: iam.IOidcProvider = (
            iam.OidcProviderNative(self, "Provider", url=GITHUB_OIDC_URL, client_ids=[STS_AUDIENCE])
            if create_provider
            else iam.OidcProviderNative.from_oidc_provider_arn(
                self,
                "Provider",
                f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:oidc-provider/{GITHUB_OIDC_HOST}",
            )
        )

        self.service_roles: dict[str, iam.Role] = {}
        for service in registry.services:
            role = iam.Role(
                self,
                f"{service.name}-Role",
                description=f"GitHub Actions in {owner}/{service.repo}: push to its ECR repo",
                # Any ref: CI pushes images for main and preview/* branches (D11).
                assumed_by=self._github_principal(f"repo:{owner}/{service.repo}:*"),
            )
            repositories[service.name].grant_push(role)
            self.service_roles[service.name] = role
            CfnOutput(
                self,
                f"{service.name}-RoleArn",
                description=f"AWS_ROLE_ARN variable for the {service.repo} GitHub repo",
                value=role.role_arn,
            )

        # Infra repo: only its main branch may deploy (every deploy, including previews,
        # runs from the infra repo's default branch: dispatch, schedule, manual run).
        self.infra_role = iam.Role(
            self,
            "InfraRole",
            description=f"GitHub Actions in {owner}/{infra_repo}: deploy through CDK",
            assumed_by=self._github_principal(f"repo:{owner}/{infra_repo}:ref:refs/heads/main"),
        )
        # 1. Deploy only through the CDK bootstrap roles (-> CloudFormation) (D32).
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:role/cdk-*-{kind}-role-*"
                    for kind in ("deploy", "file-publishing", "image-publishing", "lookup")
                ],
            )
        )
        # 2. Direct reads for the reconciler.
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                # Which pe-env-* stacks exist. These actions don't support resource scoping
                # for ListStacks, so "*" (read-only).
                actions=["cloudformation:ListStacks", "cloudformation:DescribeStacks"],
                resources=["*"],
            )
        )
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:DescribeImages"],  # commit SHA -> image digest
                resources=[repo.repository_arn for repo in repositories.values()],
            )
        )
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{PREFIX}/shared/*"
                ],
            )
        )
        CfnOutput(
            self,
            "InfraRoleArn",
            description=f"AWS_ROLE_ARN variable for the {infra_repo} GitHub repo",
            value=self.infra_role.role_arn,
        )

    def _github_principal(self, subject: str) -> iam.IPrincipal:
        return iam.WebIdentityPrincipal(
            self.provider.oidc_provider_arn,
            conditions={
                "StringEquals": {f"{GITHUB_OIDC_HOST}:aud": STS_AUDIENCE},
                "StringLike": {f"{GITHUB_OIDC_HOST}:sub": subject},
            },
        )
