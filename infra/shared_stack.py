"""`preview-baseline`: the long-lived baseline every environment builds on.

VPC, ECS cluster, one ECR repo per registered service, main's Aurora cluster (+ bootstrap),
GitHub OIDC roles. Values env stacks need are published to SSM under /preview-baseline/ (D24).
"""

from aws_cdk import Duration, Fn, RemovalPolicy, Stack, Token
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra import config
from infra.aurora_bootstrap import AuroraBootstrap
from infra.config import SsmKeys
from infra.github_oidc import GithubOidc
from reconciler.registry import Registry

AURORA_MIN_ACU = 0.5  # D13: never pauses, so reviewers never hit a cold start
# Max ACU sets max_connections, not just the compute ceiling (D40/D41). Aurora Serverless
# v2 holds max_connections constant, derived from the memory of the *max* ACU (changing it
# needs a writer reboot). AWS defaults for Aurora PostgreSQL:
#   4 ACU -> 823, 8 ACU -> 1,669, 16 ACU -> 3,360, 32+ ACU -> 5,000
# but with min capacity 0.5 ACU (D13) it is capped at 2,000. So 16 ACU would buy only ~330
# more connections than 8, at twice the cost ceiling. 8 gives ~1,669: main's services
# (<= 10 per task) plus ~80 two-service previews at the preview pool size (2 + 3 = 5 per
# task), even with every task doubled during a rolling deploy. That is past the ~50-ALB
# preview ceiling (D10). Idle cost is set by the min ACU, not the max.
# Source: Aurora User Guide, "Maximum connections for Aurora Serverless v2".
AURORA_MAX_ACU = 8
AURORA_ADMIN_USER = "preview_env_admin"


class SharedStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        registry: Registry,
        infra_repo: str,
        create_oidc_provider: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # --- Network (D9): public (ALBs), private with egress via ONE NAT (tasks),
        # isolated (Aurora, no route to the internet).
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=config.AZ_COUNT,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22
                ),
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )

        self.cluster = ecs.Cluster(self, "Cluster", vpc=self.vpc)

        # --- Images: one ECR repo per registered service (D25), named after the service.
        self.repositories: dict[str, ecr.Repository] = {}
        for service in registry.services:
            repo = ecr.Repository(
                self,
                f"{service.name}-Repo",
                repository_name=service.name,
                image_scan_on_push=True,
                # Images are rebuildable from git, so tearing down the platform (D3) removes
                # them instead of leaving orphaned repos that block a redeploy.
                removal_policy=RemovalPolicy.DESTROY,
                empty_on_delete=True,
            )
            # Lifecycle (D11). Tags are commit SHAs, which can't tell main from preview
            # images, so service CI tags main images twice: `<sha>` (how the reconciler finds
            # branch builds) and `main-<sha>` (how it finds main builds, and what this policy
            # keeps).
            # ECR semantics: an image matched by a rule's tag filter can't be expired by a
            # later (higher-numbered) rule, so rule 1 protects the newest N main images and
            # rule 2 only ever expires previews, untagged manifests and old main images.
            repo.add_lifecycle_rule(
                rule_priority=1,
                description=f"Keep the newest {config.ECR_KEEP_MAIN_IMAGES} main images",
                tag_pattern_list=[f"{config.ECR_MAIN_TAG_PREFIX}*"],
                max_image_count=config.ECR_KEEP_MAIN_IMAGES,
            )
            repo.add_lifecycle_rule(
                rule_priority=2,
                description=(
                    f"Expire everything else after {config.ECR_EXPIRE_OTHER_IMAGES_DAYS} days"
                ),
                tag_status=ecr.TagStatus.ANY,
                max_image_age=Duration.days(config.ECR_EXPIRE_OTHER_IMAGES_DAYS),
            )
            self.repositories[service.name] = repo

        # --- main's database (D6, D12, D13, D16).
        self.db_cluster = rds.DatabaseCluster(
            self,
            "Aurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_17_9
            ),
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            serverless_v2_min_capacity=AURORA_MIN_ACU,
            serverless_v2_max_capacity=AURORA_MAX_ACU,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            credentials=rds.Credentials.from_generated_secret(AURORA_ADMIN_USER),
            iam_authentication=True,
            enable_data_api=True,  # used by the bootstrap below
            storage_encrypted=True,
            backup=rds.BackupProps(retention=Duration.days(1)),
            # Default removal policy (SNAPSHOT): tearing down keeps a final snapshot of main.
        )

        # Tasks that talk to Aurora (main's services, previews' copy-db) attach this group.
        # Env stacks only *use* it; they never add rules to shared resources (D24).
        self.db_client_sg = ec2.SecurityGroup(
            self,
            "DbClientSg",
            vpc=self.vpc,
            description="Attach to tasks that connect to the main Aurora cluster",
        )
        self.db_cluster.connections.allow_default_port_from(
            self.db_client_sg, "Tasks carrying the db-client security group"
        )

        AuroraBootstrap(
            self, "AuroraBootstrap", cluster=self.db_cluster, services=registry.services
        )

        # --- Ingress allowlist (D42): every env's ALB accepts HTTP only from this list.
        # Created WITHOUT entries, so no IP is ever committed to the (public) repo;
        # scripts/allow_ip.py adds and removes entries (people, CI runners).
        # CloudFormation caveat (checked against the AWS::EC2::PrefixList resource
        # provider's UpdateHandler): CloudFormation only touches entries when it *updates*
        # this resource, and an update reconciles the entries to the template, i.e. would
        # drop every script-added entry (or fail, with Entries absent). Deploys that don't
        # change this resource leave the entries alone. So keep its properties fixed: no
        # tags, a fixed name, and don't change preview-baseline's stack-level tags (they propagate
        # to resources and would trigger an update). MaxEntries can't be updated at all.
        # L1 on purpose: the L2 `ec2.PrefixList` always renders `Entries: []`.
        self.alb_allowlist = ec2.CfnPrefixList(
            self,
            "AlbAllowlist",
            prefix_list_name=config.ALB_ALLOWLIST_NAME,
            address_family="IPv4",
            max_entries=config.ALB_ALLOWLIST_MAX_ENTRIES,
        )

        GithubOidc(
            self,
            "GithubOidc",
            registry=registry,
            repositories=self.repositories,
            infra_repo=infra_repo,
            alb_allowlist_arn=self.alb_allowlist.attr_arn,
            create_provider=create_oidc_provider,
        )

        self._publish_to_ssm()

    def _publish_to_ssm(self) -> None:
        public = self.vpc.public_subnets
        private = self.vpc.private_subnets
        # Env stacks split these lists with an assumed length of AZ_COUNT; fail synth here
        # rather than deploy an env stack that silently picks the wrong subnets.
        assert len(public) == len(private) == config.AZ_COUNT, (public, private)

        values = {
            SsmKeys.VPC_ID: self.vpc.vpc_id,
            SsmKeys.AVAILABILITY_ZONES: Fn.join(",", self.vpc.availability_zones),
            SsmKeys.PUBLIC_SUBNET_IDS: Fn.join(",", [s.subnet_id for s in public]),
            SsmKeys.PRIVATE_SUBNET_IDS: Fn.join(",", [s.subnet_id for s in private]),
            SsmKeys.ECS_CLUSTER_NAME: self.cluster.cluster_name,
            SsmKeys.DB_CLUSTER_ARN: self.db_cluster.cluster_arn,
            SsmKeys.DB_ADMIN_SECRET_ARN: self.db_cluster.secret.secret_arn,
            SsmKeys.DB_ENDPOINT: self.db_cluster.cluster_endpoint.hostname,
            SsmKeys.DB_PORT: Token.as_string(self.db_cluster.cluster_endpoint.port),
            SsmKeys.DB_RESOURCE_ID: self.db_cluster.cluster_resource_identifier,
            SsmKeys.DB_CLIENT_SECURITY_GROUP_ID: self.db_client_sg.security_group_id,
            SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID: self.alb_allowlist.attr_prefix_list_id,
        }
        for name, value in values.items():
            ssm.StringParameter(
                self,
                "Param" + name.removeprefix("/preview-baseline").replace("/", "-"),
                parameter_name=name,
                string_value=value,
            )
