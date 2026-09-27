"""One environment (D4): `main` and every preview are instances of this same construct.

Per env: one internet-facing ALB (D10). Per registered service (D25): a Fargate ARM64
service (D5) in private subnets (D9), reachable only from this env's ALB, routed by path
prefix. The database is a pluggable `EnvDatabase` (D4, D40); nothing here knows which one.
"""

import re

from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.database import EnvDatabase, ServiceTask
from infra.shared_refs import SharedRefs
from reconciler.registry import Registry, Service
from reconciler.spec import EnvSpec, ServiceSpec

APP_CONTAINER = "app"
HTTP_PORT = 80

# Fargate task size per service (shared by the app and its helper containers).
TASK_CPU = 512
TASK_MEMORY_MIB = 1024
# One task per service in every env: enough for dev and previews, and cheap. (Main can
# scale out: migrations take an advisory lock and seeding is idempotent.)
DESIRED_COUNT = 1

# Covers the containers that run before the app (copy-db + migrate in previews) plus
# startup, so the ALB doesn't fail health checks on a task that is still preparing.
HEALTH_CHECK_GRACE = Duration.minutes(3)


class AppEnvironment(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        spec: EnvSpec,
        registry: Registry,
        shared: SharedRefs,
        database: EnvDatabase,
    ) -> None:
        super().__init__(scope, id)
        _check_spec_matches_registry(spec, registry)

        # D42: HTTP only from the shared allowlist prefix list. Adding or removing an entry
        # there takes effect on every env at once, with no redeploy.
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=shared.vpc,
            description="Env ALB: HTTP from the pe-alb-allowlist prefix list only",
            allow_all_outbound=False,  # egress to the tasks is added per target below
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(shared.alb_allowlist_prefix_list_id),
            ec2.Port.tcp(HTTP_PORT),
            "HTTP from the ALB allowlist (D42)",
        )
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=shared.vpc,
            internet_facing=True,  # public subnets, but only allowlisted IPs get in
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        self.listener = self.alb.add_listener(
            "Http",
            port=HTTP_PORT,
            open=False,  # no 0.0.0.0/0 rule: ingress comes only from the allowlist above
            default_action=elbv2.ListenerAction.fixed_response(
                404, content_type="text/plain", message_body="No service at this path\n"
            ),
        )

        self.services: dict[str, ecs.FargateService] = {}
        for index, service in enumerate(registry.services):
            self.services[service.name] = self._add_service(
                service,
                spec.services[service.name],
                env_name=spec.env,
                shared=shared,
                database=database,
                # Registry order -> stable, unique rule priorities.
                priority=(index + 1) * 10,
            )

        self.url = f"http://{self.alb.load_balancer_dns_name}"
        CfnOutput(self, "Url", value=self.url, description=f"Base URL of env {spec.env}")

    def _add_service(
        self,
        service: Service,
        service_spec: ServiceSpec,
        *,
        env_name: str,
        shared: SharedRefs,
        database: EnvDatabase,
        priority: int,
    ) -> ecs.FargateService:
        scope = Construct(self, service.name)

        task_definition = ecs.FargateTaskDefinition(
            scope,
            "Task",
            cpu=TASK_CPU,
            memory_limit_mib=TASK_MEMORY_MIB,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        # The reconciler resolved the image to `<registry>/<service>@sha256:...` (D11). Pin
        # it to this service's own ECR repo in pe-shared: an EnvSpec can't point a service
        # at someone else's image, and CDK grants the pull permission for exactly this repo.
        image = ecs.ContainerImage.from_ecr_repository(
            ecr.Repository.from_repository_name(scope, "Repo", service.name),
            tag=image_tag_or_digest(service, service_spec.image),
        )

        log_group = logs.LogGroup(
            scope,
            "Logs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,  # D14: logs go with the env
        )
        task = ServiceTask(
            service=service,
            task_definition=task_definition,
            image=image,
            base_environment={
                "SERVICE_NAME": service.name,
                "ENV_NAME": env_name,
                "PATH_PREFIX": service.path_prefix,
                "PORT": str(service.port),
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix=service.name, log_group=log_group),
        )

        wiring = database.wire(task)

        app = task_definition.add_container(
            APP_CONTAINER,
            image=image,
            # No command: the image default is `serve`.
            essential=True,
            environment={**task.base_environment, **wiring.environment},
            logging=task.logging,
            port_mappings=[ecs.PortMapping(container_port=service.port)],
        )
        app.add_container_dependencies(*wiring.app_depends_on)

        task_sg = ec2.SecurityGroup(
            scope,
            "TaskSg",
            vpc=shared.vpc,
            description=f"{service.name} tasks: inbound only from this env ALB",
        )
        fargate_service = ecs.FargateService(
            scope,
            "Service",
            cluster=shared.cluster,
            task_definition=task_definition,
            desired_count=DESIRED_COUNT,
            min_healthy_percent=100,
            max_healthy_percent=200,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[task_sg, *wiring.security_groups],
            # D14: a deploy whose tasks never get healthy fails in minutes and rolls back,
            # instead of CloudFormation waiting up to 3 hours.
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            health_check_grace_period=HEALTH_CHECK_GRACE,
        )
        # E.g. a preview's database: created before the service, and on stack deletion
        # dropped only after the service (and its tasks) are gone (D40).
        for dependency in wiring.service_depends_on:
            fargate_service.node.add_dependency(dependency)

        # `add_targets` also opens the task SG to the ALB's SG on the service port (only).
        # CDK wires the ALB's egress to *every* SG on the service, so the ALB also gets a
        # (harmless) egress rule toward the shared db-client SG, which has no ingress rules;
        # no ingress is added to that SG because it is imported as immutable.
        self.listener.add_targets(
            service.name,
            priority=priority,
            conditions=[
                elbv2.ListenerCondition.path_patterns(
                    [service.path_prefix, f"{service.path_prefix}/*"]
                )
            ],
            port=service.port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[
                fargate_service.load_balancer_target(
                    container_name=APP_CONTAINER, container_port=service.port
                )
            ],
            health_check=elbv2.HealthCheck(
                # DB-free endpoint (D15): a DB blip must not take every task out of the ALB.
                path=f"{service.path_prefix}{service.health_path}",
                healthy_http_codes="200",
                interval=Duration.seconds(15),
                healthy_threshold_count=2,
            ),
            deregistration_delay=Duration.seconds(10),  # faster deploys/teardowns
        )
        return fargate_service


# `[<registry>/]<repo>@sha256:<64 hex>` or `[<registry>/]<repo>:<tag>`
_IMAGE_REF = re.compile(
    r"^(?:.*/)?(?P<repo>[a-z0-9._-]+)"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64})|:(?P<tag>\w[\w.-]{0,127}))$"
)


def image_tag_or_digest(service: Service, image: str) -> str:
    """The digest (or tag) of an EnvSpec image, checked against the service's ECR repo."""
    match = _IMAGE_REF.match(image)
    if not match or match["repo"] != service.name:
        raise ValueError(
            f"image {image!r} for {service.name!r} must be <registry>/{service.name}@sha256:... "
            f"(or :<tag>) from the platform's ECR repo"
        )
    return match["digest"] or match["tag"]


def _check_spec_matches_registry(spec: EnvSpec, registry: Registry) -> None:
    """Every env runs every registered service, each with exactly one resolved image."""
    registered = {s.name for s in registry.services}
    in_spec = set(spec.services)
    if registered != in_spec:
        raise ValueError(
            f"EnvSpec for {spec.env!r} doesn't match the registry: "
            f"missing {sorted(registered - in_spec)}, unknown {sorted(in_spec - registered)}"
        )
