"""CDK app for the preview environments platform.

- `pe-shared` (SharedStack): VPC, ECS cluster, ECR repos, Aurora, GitHub OIDC roles.
- `pe-env-<env>` (EnvStack): one environment (main or a preview group), built from an EnvSpec.
"""
