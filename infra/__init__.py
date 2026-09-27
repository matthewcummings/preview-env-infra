"""CDK app for the preview environments platform.

- `preview-baseline` (SharedStack): VPC, ECS cluster, ECR repos, Aurora, GitHub OIDC roles.
- `preview-env-<env>` (EnvStack): one environment (main or a preview group), built from an EnvSpec.
"""
