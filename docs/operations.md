# Operations (stub, to fold into the README)

Everything runs through `make` (thin wrappers around Python scripts and the reconciler
CLI) or GitHub Actions. `make help` lists the targets.

## First-time setup (from a laptop, admin credentials)

| Step | Command | What it does |
|---|---|---|
| 1 | `make doctor` | Checks uv, docker, node/npx, aws CLI, AWS credentials, region, CDK bootstrap, GitHub owner. Read-only. |
| 2 | `make bootstrap` | `npx cdk bootstrap` for the current account/region (once). |
| 3 | `make deploy-baseline ALLOW_MY_IP=1` | Deploys `preview-baseline`. Reuses the account's GitHub OIDC provider if one exists and `preview-baseline` doesn't own it (D39). Then adds your public IP to the ALB allowlist (D42). |
| 4 | `export INFRA_DISPATCH_TOKEN=...` then `make setup-github` | Sets repo variables (`AWS_REGION`, `AWS_ROLE_ARN`, `INFRA_REPO` on each service repo; `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN` on the infra repo) from `preview-baseline` outputs, and the dispatch secret on the service repos. Without the token it prints how to create one. `DRY_RUN=1` prints the `gh` commands only. |
| 5 | `make deploy-main` | Reconciles the `main` env (each service's newest `main` commit that has an image). After this, CI takes over. |

## Day to day

| Command | What it does |
|---|---|
| `make plan BRANCH=preview/<group>/...` (or `GROUP=<group>`; default `main`) | Prints the plan. `NO_AWS=1`: SHAs only, no AWS needed. |
| `make preview BRANCH=preview/<group>/...` | Reconciles that branch's env by hand: create, update, or tear down if no branches remain. Same as the CI run. |
| `make teardown GROUP=<group>` | Deletes the env's stack now (CloudFormation DeleteStack, D35), even if branches still exist (it warns: the next push recreates it). For missed delete events (D21/D29). Never `main`. |
| `make smoke ENV=<env> [SPEC=envspec.json]` | Smoke test (D34): `/healthz`, `/readyz`, `/version` SHA (with `SPEC`), CRUD round trip, and for previews an isolation check against `main`. Needs your IP on the allowlist. |
| `make allow-ip [CIDR=...]` / `make disallow-ip [CIDR=...]` / `make list-ips` | Manage the ALB allowlist (D42). Default: your current public IP /32. |
| `make test` / `make lint` / `make synth` | Tests, ruff, credential-free `cdk synth`. |

## GitHub Actions (infra repo)

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | Pull requests; pushes to branches other than `main`; called by `platform.yml` | ruff check + format check, pytest, credential-free `cdk synth` (D38). |
| `platform.yml` | Push to `main`, manual | `ci.yml`, then deploy `preview-baseline` (queue `preview-baseline`), then reconcile the `main` env (queue `preview-env-main`) and smoke-test it with the runner's IP temporarily allowlisted (always removed). |
| `reconcile.yml` | `repository_dispatch` `service-changed` from a service repo (push to `main`/`preview/*`, or a `preview/*` delete); manual with a `branch` input | Job `plan` (read-only) finds the env and action. Job `apply` runs only if something changes, in queue `preview-env-<env>`, recomputes the plan fresh (D24), applies it, and smoke-tests creates/updates with the runner-IP allowlist dance. |

Conflicts, missing images with no `main` fallback, and blocked stacks fail the `plan` job
with the reason in the job summary; nothing is applied. Ignored branch names end the run
green with rename instructions in the summary (D7).

## Not built (documented TODOs, D29)

Nightly sweep and age limit, reconciling every env when `main` moves, cap on concurrent
previews, stale-branch warnings, PR comments with the env URL.
