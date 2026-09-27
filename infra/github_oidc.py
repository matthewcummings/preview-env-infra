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

from infra.config import ENV_STACK_PREFIX, SHARED_STACK_NAME
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
        alb_allowlist_arn: str,
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
                assumed_by=self._github_principal(owner, service.repo, "*"),
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
            assumed_by=self._github_principal(owner, infra_repo, "ref:refs/heads/main"),
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
                # Which preview-env-* stacks exist. These actions don't support resource scoping
                # for ListStacks, so "*" (read-only).
                actions=["cloudformation:ListStacks", "cloudformation:DescribeStacks"],
                resources=["*"],
            )
        )
        stack_arn = f"arn:{Aws.PARTITION}:cloudformation:{Aws.REGION}:{Aws.ACCOUNT_ID}:stack"
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                # Teardown deletes env stacks directly (D35); events show its progress.
                # The stack's own CDK execution role removes the resources, so no PassRole.
                actions=["cloudformation:DeleteStack", "cloudformation:DescribeStackEvents"],
                resources=[f"{stack_arn}/{ENV_STACK_PREFIX}*/*"],
            )
        )
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                # deploy_baseline.py: does preview-baseline manage the GitHub OIDC provider? (D39)
                actions=["cloudformation:ListStackResources"],
                resources=[f"{stack_arn}/{SHARED_STACK_NAME}/*"],
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
                    f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter/{SHARED_STACK_NAME}/*"
                ],
            )
        )
        # 3. CI smoke tests (D34/D42): add the runner's IP to the ALB allowlist, then remove
        # it. Writes only to that one prefix list.
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:ModifyManagedPrefixList", "ec2:GetManagedPrefixListEntries"],
                resources=[alb_allowlist_arn],
            )
        )
        self.infra_role.add_to_policy(
            iam.PolicyStatement(
                # Describe* calls don't support resource-level permissions.
                actions=["ec2:DescribeManagedPrefixLists"],
                resources=["*"],
            )
        )
        CfnOutput(
            self,
            "InfraRoleArn",
            description=f"AWS_ROLE_ARN variable for the {infra_repo} GitHub repo",
            value=self.infra_role.role_arn,
        )

    def _github_principal(self, owner: str, repo: str, ref: str) -> iam.IPrincipal:
        return iam.WebIdentityPrincipal(
            self.provider.oidc_provider_arn,
            conditions={
                "StringEquals": {f"{GITHUB_OIDC_HOST}:aud": STS_AUDIENCE},
                "StringLike": {f"{GITHUB_OIDC_HOST}:sub": github_subjects(owner, repo, ref)},
            },
        )


def github_subjects(owner: str, repo: str, ref: str) -> list[str]:
    """The OIDC `sub` claims a role accepts for `owner/repo` at `ref` (e.g. `*`).

    GitHub sends one of two formats: the classic `repo:owner/repo:...`, or the newer one
    with immutable IDs, `repo:owner@<owner id>/repo@<repo id>:...` (the IDs stop a deleted
    and re-created repo with the same name from inheriting the trust). Both are accepted,
    with the IDs as wildcards, so the check is owner name + repo name + ref either way.
    The `@` right after the owner and before the repo keeps look-alike names from matching
    (e.g. `owner-evil`). Pinning the actual IDs is a hardening TODO.
    """
    return [f"repo:{owner}/{repo}:{ref}", f"repo:{owner}@*/{repo}@*:{ref}"]
