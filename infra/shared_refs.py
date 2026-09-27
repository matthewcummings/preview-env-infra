"""What an env stack needs from `preview-baseline`, read from SSM at deploy time (D24).

`ssm.StringParameter.value_for_string_parameter` becomes a CloudFormation parameter of type
`AWS::SSM::Parameter::Value<String>`: CloudFormation resolves it during deploy, so synth
does no lookups and needs no AWS credentials, and there is no export locking `preview-baseline`.

The sharp edge: those values are opaque tokens at synth time, and constructs like
`Vpc.from_vpc_attributes` need *lists* (AZs, subnet IDs) whose length they know. We store
each list as one comma-separated string and split it with `Fn.split(..., assumed_length)`,
which yields exactly AZ_COUNT `Fn::Select` tokens. `preview-baseline` asserts the same AZ_COUNT
when it writes the lists, so both sides agree by construction.
"""

from dataclasses import dataclass

from aws_cdk import Acknowledgment, Fn, Stack, Validations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra import config
from infra.config import SsmKeys


@dataclass(frozen=True)
class SharedRefs:
    vpc: ec2.IVpc
    cluster: ecs.ICluster
    db_cluster_arn: str  # token
    db_admin_secret_arn: str  # token
    db_endpoint: str  # token
    db_port: str  # token
    db_resource_id: str  # token, e.g. cluster-ABC123; used in rds-db:connect ARNs
    db_client_sg: ec2.ISecurityGroup
    alb_allowlist_prefix_list_id: str  # token (D42)

    @classmethod
    def from_ssm(cls, scope: Construct) -> SharedRefs:
        def value(name: str) -> str:
            return ssm.StringParameter.value_for_string_parameter(scope, name)

        def split(name: str) -> list[str]:
            return Fn.split(",", value(name), config.AZ_COUNT)

        vpc = ec2.Vpc.from_vpc_attributes(
            scope,
            "SharedVpc",
            vpc_id=value(SsmKeys.VPC_ID),
            availability_zones=split(SsmKeys.AVAILABILITY_ZONES),
            public_subnet_ids=split(SsmKeys.PUBLIC_SUBNET_IDS),
            private_subnet_ids=split(SsmKeys.PRIVATE_SUBNET_IDS),
        )
        # The AZ list only satisfies the VpcAttributes API: nothing in an env stack selects
        # subnets by AZ, so its CloudFormation parameter ends up unreferenced. Keep it anyway
        # so the imported VPC never carries made-up AZ names if something starts using them.
        Validations.of(Stack.of(scope)).acknowledge(
            Acknowledgment(
                id="CloudFormation-Validate::W2001",
                reason="SSM parameter for the VPC's AZs is intentionally unreferenced",
            )
        )
        # Env stacks never touch routing, so route table IDs aren't published or imported.
        Validations.of(vpc).acknowledge(
            Acknowledgment(
                id="Construct-Annotations::@aws-cdk/aws-ec2:noSubnetRouteTableId",
                reason="Env stacks don't read subnet route tables",
            )
        )
        cluster = ecs.Cluster.from_cluster_attributes(
            scope,
            "SharedCluster",
            cluster_name=value(SsmKeys.ECS_CLUSTER_NAME),
            vpc=vpc,
        )
        db_client_sg = ec2.SecurityGroup.from_security_group_id(
            scope,
            "SharedDbClientSg",
            value(SsmKeys.DB_CLIENT_SECURITY_GROUP_ID),
            # Owned by preview-baseline: env stacks must not add rules to it (D24).
            mutable=False,
        )
        return cls(
            vpc=vpc,
            cluster=cluster,
            db_cluster_arn=value(SsmKeys.DB_CLUSTER_ARN),
            db_admin_secret_arn=value(SsmKeys.DB_ADMIN_SECRET_ARN),
            db_endpoint=value(SsmKeys.DB_ENDPOINT),
            db_port=value(SsmKeys.DB_PORT),
            db_resource_id=value(SsmKeys.DB_RESOURCE_ID),
            db_client_sg=db_client_sg,
            alb_allowlist_prefix_list_id=value(SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID),
        )
