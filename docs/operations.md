# Operations reference

The README covers setup from scratch. This page is the reference for the `make` targets and the GitHub Actions workflows. Every target is a thin wrapper around a Python script or the reconciler CLI (`uv run python -m reconciler`), so it behaves the same on macOS, Linux and WSL2. `make help` lists them.

Names used below: the baseline stack is `preview-baseline` (SSM parameters under `/preview-baseline/...`, ALB allowlist `preview-baseline-alb-allowlist`); each env is a `preview-env-<name>` stack, e.g. `preview-env-main`, `preview-env-checkout` (D43).

## Make targets

### Setup (once, with admin AWS credentials)

| Target | What it does |
|---|---|
| `make doctor` | Read-only preflight: tools, AWS credentials and region, CDK bootstrap, GitHub owner, repos, Actions variables and secrets, auto-delete head branches, and whether the tokens can reach their repos. PASS / WARN (a later step hasn't run yet) / FAIL (fix first) / SKIP. Exits 1 only on FAIL (D46). |
| `make bootstrap` | `cdk bootstrap` for the current account and region. |
| `make deploy-baseline [ALLOW_MY_IP=1]` | Deploys `preview-baseline` via `scripts/deploy_baseline.py`, reusing the account's GitHub OIDC provider when needed (D39). `ALLOW_MY_IP=1` then adds your public IP to the ALB allowlist (D42). |
| `make setup-github [DRY_RUN=1]` | From `preview-baseline`'s outputs, sets the Actions variables on all three repos (`AWS_REGION`, `AWS_ROLE_ARN`, `INFRA_REPO` on the service repos; `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN` on the infra repo), stores `INFRA_DISPATCH_TOKEN` in the service repos and `PREVIEW_COMMENT_TOKEN` in the infra repo (read from your environment), and turns on "Automatically delete head branches" (D44). `DRY_RUN=1` prints the `gh` commands only. |
| `make deploy-main` | Reconciles the `main` env from your laptop (each service's newest `main` commit that has an image). CI normally does this. |

### Day to day

| Target | What it does |
|---|---|
| `make plan BRANCH=preview/<group>/...` (or `GROUP=<group>`; default `main`) | Prints the plan without changing anything. `NO_AWS=1` shows the branch-to-service decisions using only GitHub. |
| `make preview BRANCH=preview/<group>/...` | Reconciles that branch's env by hand: create, update, or tear down if no branches remain. Same as the CI run. |
| `make url ENV=<env>` | Prints an env's URL (its stack's `Url` output). Needs AWS access. |
| `make smoke ENV=<env> [SPEC=envspec.json]` | Smoke test (D34): health, readiness, CRUD round trip, and for previews an isolation check against `main`. With `SPEC`, also checks each service runs the planned commit. Your IP must be on the allowlist. |
| `make teardown GROUP=<group>` | Deletes a preview's stack now with CloudFormation `DeleteStack` (D35), even if its branches still exist (the next push recreates it). For a missed delete event. Never `main`. |
| `make allow-ip [CIDR=...]` / `make disallow-ip [CIDR=...]` / `make list-ips` | Manage the ALB allowlist (D42). Default: your current public IP as a /32. |

### Development

| Target | What it does |
|---|---|
| `make test` | All tests: reconciler, CDK template assertions, scripts. No AWS, no network. |
| `make lint` | `ruff check` + `ruff format --check`. |
| `make synth` | `cdk synth`, no AWS credentials needed. |

## GitHub Actions

### Service repos (`service-a`, `service-b`): `ci.yml`

| Trigger | What it does |
|---|---|
| Push to any branch | Lint, tests (against a throwaway Postgres), single Alembic head check, Docker build. |
| Push to `main` or `preview/**`, checks passed | Pushes an ARM64 image to the service's ECR repo, tagged with the full commit SHA (plus `main-<sha>` on `main`), then sends `repository_dispatch` (`service-changed`) to the infra repo. |
| Delete of a `preview/**` branch | Sends `service-changed` to the infra repo, which falls back to `main` or tears the env down. |

Without the `INFRA_DISPATCH_TOKEN` secret, the image is still published and the signal is skipped with a notice (deploy by hand with `make preview BRANCH=...`).

### Infra repo (`preview-env-infra`)

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | Pull requests, pushes to branches other than `main`, and called by `platform.yml` | Ruff, pytest, credential-free `cdk synth` (D38). |
| `platform.yml` | Push to `main`, manual | `ci.yml`, then deploy `preview-baseline` (queue `preview-baseline`), then reconcile the `main` env (queue `preview-env-main`) and smoke-test it. Deploy jobs are skipped until `AWS_DEPLOY_ROLE_ARN` is set. |
| `reconcile.yml` | `repository_dispatch` `service-changed`, or manual with a `branch` input | See below. |

`reconcile.yml` in order:

1. **`plan`** (read-only): works out the env and the action. Conflicts, blocked stacks and other refusals fail here with the reason in the job summary, and nothing is applied. Ignored branch names end green with rename instructions (D7). If a service has no image yet, the action is `wait` and the run ends cleanly (D45).
2. **`apply`**, only if something would change, in the env's queue (`preview-env-<env>`, at most one running and one waiting, D24). It recomputes the plan fresh, then creates, updates or deletes the stack.
3. **Smoke test** after a create or update: adds the runner's IP to the allowlist, runs the smoke test against the deployed commits, and always removes the IP (D34, D42).
4. **PR comment** for previews (not `main`), after a successful apply: keeps one comment per open service-repo PR in the group with the URL, each service's branch and SHA, the smoke result and a run link; on teardown, it marks existing comments as removed. It runs even if the smoke test failed, skips with a notice without `PREVIEW_COMMENT_TOKEN`, and never fails the run (D44).

## Not built (D29)

Nightly sweep and age limit, reconciling every env when `main` moves, a cap on concurrent previews, stale-branch warnings, `cdk diff` on infra PRs, alarms on the shared cluster, and a "reset preview data" workflow. Details in [`decisions.md`](decisions.md#d29-build-the-must-haves-document-the-rest).
