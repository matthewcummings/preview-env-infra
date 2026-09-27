# Design decisions

## Summary

This repo builds per-branch preview environments for two containerized FastAPI + Postgres services (`service-a`, `service-b`) on AWS, with CDK in Python. The main choices:

- **Feature groups are a branch naming convention.** Push `preview/<group>/...` in any service repo and you get an env named `<group>`. Every service with a branch in that group runs its branch; every other service runs `main`. One rule covers all four scenarios in the prompt (D7).
- **A stateless reconciler decides what each env runs.** Desired state is read live from GitHub (which branches exist), actual state from CloudFormation (which env stacks exist). There is no state file or database to drift. Every event just means "go look again" (D8, D26).
- **Compute is ECS Fargate (ARM64) behind one ALB per env**, in private subnets, with the ALBs IP-allowlisted through a managed prefix list (D5, D9, D10, D42).
- **Data: one shared Aurora Serverless v2 Postgres cluster, a logical database per service, and per env.** Each preview gets its own databases on main's cluster, copied from main on first start and kept across pushes. Every login uses IAM database auth, so there are no DB passwords in the apps (D12, D16, D40, D41).
- **CI/CD is GitHub Actions + OIDC.** Service repos test, build an image tagged with the commit SHA and signal the infra repo. Only the infra repo deploys, through CDK, with one queue per env (D11, D24, D32).
- **Scope:** the prompt's scenarios, teardown, the database copy and the plumbing around them are built. Sweeps, caps, production and a few conveniences are documented as next steps (D29).

## How to read this

- Decisions are grouped by topic, not in the order I made them. The IDs (D1-D42) are stable because code comments refer to them (for example `# D40` in `infra/database.py`).
- Each entry gives what I chose, what else I considered, why, and the consequences or limits.
- Where I changed my mind, only the final decision is shown, with a one-line **Revised:** note saying what changed and why. Rejected alternatives are kept briefly, because they explain the choice.
- **Status** in the index: **Built** (in the code), **Partly built** (core built, some pieces documented), **Documented only** (a design, not code), **Principle** (a guideline that shaped the rest).

## Index

| ID | Decision | Section | Status |
|---|---|---|---|
| D1 | Optimize for reviewers who may never deploy it | [Reviewer experience](#reviewer-experience) | Principle |
| D2 | Public repos with generic names | [Reviewer experience](#reviewer-experience) | Built |
| D3 | Keep the deployment running after submission | [Reviewer experience](#reviewer-experience) | Built (revised by D42) |
| D4 | One environment construct for `main` and previews | [Compute and networking](#compute-and-networking) | Built |
| D5 | Compute: ECS Fargate on ARM64 | [Compute and networking](#compute-and-networking) | Built |
| D6 | Each env's database is a copy of main's | [Data](#data) | Built (revised by D40) |
| D7 | Feature groups via a branch naming convention | [Feature groups](#feature-groups) | Partly built |
| D8 | A stateless reconciler decides what each env runs | [Feature groups](#feature-groups) | Built |
| D9 | Private subnets and a single NAT gateway | [Compute and networking](#compute-and-networking) | Built |
| D10 | One ALB per env, path-routed | [Compute and networking](#compute-and-networking) | Built |
| D11 | GitHub Actions + OIDC, dispatch to the infra repo | [CI/CD and security](#cicd-and-security) | Built |
| D12 | "Shared database" = shared cluster, logical DB per service | [Interpreting the prompt](#interpreting-the-prompt) | Built |
| D13 | Main's database never pauses | [Data](#data) | Built |
| D14 | Fail fast, fail loudly, fail clearly | [Reliability](#reliability) | Built |
| D15 | Tiny CRUD app with split health checks | [Reliability](#reliability) | Built |
| D16 | IAM database auth everywhere | [Data](#data) | Built |
| D17 | Alembic per service, migrations run before the app | [Data](#data) | Built |
| D18 | `main` is shared dev, not production | [Scope and what's next](#scope-and-whats-next) | Documented only |
| D19 | Seed command per service; previews copy from main | [Data](#data) | Built |
| D20 | Bring your own AWS account | [Reviewer experience](#reviewer-experience) | Built |
| D21 | Nightly sweep and age limit | [Scope and what's next](#scope-and-whats-next) | Documented only |
| D22 | Three repos: two services + this infra repo | [Interpreting the prompt](#interpreting-the-prompt) | Built |
| D23 | Previews trigger on push; branches are the source of truth | [Feature groups](#feature-groups) | Built |
| D24 | Race conditions: one stack per group + queued deploys | [Feature groups](#feature-groups) | Partly built |
| D25 | Service registry: nothing hardcodes "A and B" | [Feature groups](#feature-groups) | Built |
| D26 | Teardown when no branches remain; partial deletes fall back to `main` | [Feature groups](#feature-groups) | Partly built |
| D27 | Naming: "preview environments" | [Reviewer experience](#reviewer-experience) | Principle |
| D28 | Supporting infra (queues, caches) lives in each env's stack | [Scope and what's next](#scope-and-whats-next) | Documented only |
| D29 | Build the must-haves; document the rest as TODOs | [Scope and what's next](#scope-and-whats-next) | Principle |
| D30 | No Docker Compose; tests use testcontainers | [Scope and what's next](#scope-and-whats-next) | Built |
| D31 | Setup doc tested from scratch | [Reviewer experience](#reviewer-experience) | Principle |
| D32 | Admin for the deployer; CI provisions only through CDK | [CI/CD and security](#cicd-and-security) | Built |
| D33 | Supported OSes: macOS, Linux, Windows via WSL2 | [Scope and what's next](#scope-and-whats-next) | Principle |
| D34 | Platform smoke test after every deploy | [Reliability](#reliability) | Built |
| D35 | Teardown deletes the CloudFormation stack directly | [CI/CD and security](#cicd-and-security) | Built |
| D36 | Strict group names, no silent conversion | [Feature groups](#feature-groups) | Built |
| D37 | Previews copy main's data; keeping main clean is main's job | [Data](#data) | Built |
| D38 | Infra repo flow: PR checks, merge deploys shared + `main` | [CI/CD and security](#cicd-and-security) | Built (`cdk diff` on PRs is a TODO) |
| D39 | Reuse an existing GitHub OIDC provider | [CI/CD and security](#cicd-and-security) | Built |
| D40 | Preview DBs live on main's Aurora cluster | [Interpreting the prompt](#interpreting-the-prompt) | Built |
| D41 | Keep one shared cluster; document the scaling path | [Interpreting the prompt](#interpreting-the-prompt) | Built (scaling path documented) |
| D42 | IP allowlisting on every ALB via a managed prefix list | [Compute and networking](#compute-and-networking) | Built |

---

## Interpreting the prompt

Three phrases in the prompt needed an interpretation before anything else could be designed.

### D12. "Shared database" = a shared cluster with a logical database per service

- **Chose:** One Aurora cluster. Each service gets its own logical database (`service_a`, `service_b`) and its own role, with `REVOKE CONNECT ... FROM PUBLIC` on each database. Each service keeps its own Alembic history. If A ever needs B's data, it goes through B's HTTP API.
- **Why:** I believe in strong service boundaries. Sharing a server is fine; sharing tables is not. Postgres enforces this boundary itself (no cross-database queries without dblink or FDW), it still honors the prompt's shared server, and moving a service to its own cluster later is a connection-string change.
- **Considered:**
  - Shared tables: no boundary at all. Rejected.
  - One database with a schema per service: acceptable, but cross-schema access is one `GRANT` away.
  - A cluster per service: strongest, but it changes the prompt's premise. See D41.
- **Revised:** The first version used a Secrets Manager password per service. D16 replaced passwords with IAM auth.

### D40. Preview databases live on main's Aurora cluster ("db replica for each")

The prompt asks for each env to include a "db replica". I read that as: each env gets its own copy of the data, isolated from the others.

- **Chose:** Each preview gets its own logical databases on the existing cluster (`service_a__<group>`, `service_b__<group>`), each with a dedicated login user.
  - Created and dropped with the env's stack by CloudFormation custom resources that call the RDS Data API (`DROP DATABASE ... WITH (FORCE)` on teardown). The ECS services depend on the database resource, so CloudFormation stops the tasks before the drop.
  - Each preview user can connect only to its own databases; IAM `rds-db:connect` is scoped to that user.
  - Each preview user is throttled: `CONNECTION LIMIT 20` and `statement_timeout = 30s`, so one preview can't starve `main` or the others.
- **Why:** Preview data should survive pushes. Logical databases on the existing cluster persist naturally, add no new resource types, create in seconds, and extend D12's model one level: shared server, a logical database per service, now per env too. Previews also run on the same engine as `main` (Aurora), so there is no engine-parity gap.
- **Revised:** My first design (D6) gave each preview task a Postgres sidecar container, seeded from main at startup. That made previews fast to create, but every push replaced the task and wiped the preview's data (add test data, push a fix, and it's gone). Before that, I had considered an Aurora clone per preview.
- **Considered:**
  - An Aurora copy-on-write clone per preview: a real replica, but ~10+ minutes to create each one. The right answer once data gets large.
  - Postgres sidecar per task (my earlier design): fast, but data is lost on every deploy.
  - Sidecar + EFS for persistence: more infra to clean up, Postgres over NFS, and stop-before-start deploys.
  - A separate Aurora cluster just for previews: another ~15 minutes of first-time setup; a later step at scale (D41).
- **Accepted downsides:**
  1. Previews share CPU, memory and connections with `main`. Mitigated by the per-user limits above and smaller preview connection pools (D41).
  2. A weaker security boundary than separate servers. Acceptable for dev and preview data; production data never lives here (D18).
  3. One cluster is a single point of failure for `main` and every preview. CloudWatch alarms on the cluster are a TODO.
  4. Cleanup relies on the drop custom resource. A failure leaves an orphaned database, which has to be found and dropped by hand until the nightly sweep (D21) exists.
  5. Preview data drifts from `main` over time. Schema stays consistent (merging `main` into the branch brings its migrations). A "reset preview data" workflow is a TODO; for now, delete and re-push the branch.

### D41. Keep one shared cluster, and document the scaling path

- **Considered:** A cluster per service. Strongest blast-radius isolation, mirrors team ownership, and first-time setup takes the same time (clusters create in parallel).
- **Chose:** One shared cluster. The prompt describes "microservices with a shared database"; the task is preview environments for that setup, not a redesign of the data layer. Separate clusters mostly buy blast-radius isolation, which matters much more in production than in dev. They also don't scale down gracefully (35 services = 35 clusters, each with a minimum cost, maintenance windows and setup).
- **Built now:** Smaller connection pools for previews (`pool_size=2, max_overflow=3`, versus main's 5 + 5).
- **Scaling path, with the math:**
  - Every env runs every service, so logical databases = envs x services. 5 services x 20 previews = 100 databases.
  - **Connections break first, not the database count.** Postgres handles hundreds of databases fine, but 100 databases x up to 10 pooled connections is ~1,000 connections on one cluster.
  - Fixes, in order:
    1. Smaller preview pools + per-user `CONNECTION LIMIT` (both built).
    2. **Partial environments:** only services with a branch get a preview database and task; everything else is served by `main`. Databases then grow with branches, not envs x services. This is the real fix (see D7).
    3. RDS Proxy: shares a small set of real connections across many clients.
    4. A cluster per service, or a few clusters for groups of services. The design makes this a config change: the database component is handed a cluster. In a real platform I'd push for this as teams grow.

### D22. Three repos: two services plus one infra repo

- **Chose:** `service-a` and `service-b` are the prompt's "2 repos, each containing a basic FastAPI Postgres CRUD application". `preview-env-infra` is the prompt's CDK project: the shared infra, the `main` env and the reconciler. It is the entry point for the submission and links to the other two.
- **Why:** A preview needs both services deployed together, so neither service repo can own it. Shared infra in one service's repo means one team owns everyone's infra and its CI runs on unrelated changes. This matches D12: each service owns its code and data; the platform owns what they share. Inside a company this repo would probably be called `dev-infra`.
- **Considered:**
  - CDK inside one app repo: lopsided ownership.
  - A monorepo: breaks "2 repos" and removes the cross-repo branch events that feature groups are about.

---

## Feature groups

The core of the take-home: deciding which branches of which services run together.

### D7. Feature groups are a branch naming convention

- **Chose:** `preview/<group>[/<description>]`. A branch like that in any service repo joins env `<group>`, together with every other repo's branch in the same group. The description is optional.
  - `preview/checkout/cart-api` in A + `preview/checkout/schema` in B -> one env, `checkout`.
  - `preview/login-fix` -> an env of its own (a group of one).
  - `feature/whatever` -> no env.
- **One rule, no special cases.** A solo feature is a group of one. The prompt's four scenarios all follow:

  | Branches | Env(s) |
  |---|---|
  | A only | A's branch + B's `main` |
  | B only | A's `main` + B's branch |
  | A and B, same group | one env with both branches |
  | A and B, different groups | two envs, each paired with the other service's `main` |

- **The env is named after the group, not the branch,** so when a second repo joins a group later, the existing env updates in place.
- **Branches outside the convention are loudly ignored:** no env, and the run summary explains why and suggests a rename. Bot branches (`dependabot/*`, `renovate/*`) are ignored quietly. Opting out is the default: don't use `preview/`.
- **Conflict rule:** Sharing a group across repos is deliberate. Two branches in the *same* repo claiming one group is ambiguous, so the reconciler refuses, names both branches, and leaves the env unchanged.
- **Plan output:** Every run prints a `terraform plan`-style summary (the env, what triggered it, per service the matched branch or "none -> main" with its SHA, and the action) to the log and the GitHub job summary.
- **Considered:**
  - Git tags: collide between developers, are meant as fixed markers, and don't disappear on merge, so the teardown signal is lost.
  - PR labels: grouping lives in GitHub rather than git, needs an open PR, and label changes need their own trigger.
  - Identical branch names across repos with no prefix: groups by accident (two repos' `fix-tests`).
  - `feature/<group>`: developers already type `feature/...` out of habit, same accidental-grouping problem.
  - `fg/`: unambiguous but cryptic. `preview/` explains itself.
- **Consequences and limits:**
  - Reusing an old branch name is harmless: images are looked up by commit SHA, never by branch name, and teardown deletes everything.
  - The real risk is stale branches that are never deleted. I recommend "Automatically delete head branches" in each repo, so merged = deleted. A stale-branch warning in the plan output is a TODO (D29).
  - **Scaling beyond a handful of services:** matching stays cheap (one GitHub API call per repo), but copying the whole system per env does not: 35 services per env when a feature touches 2. At that scale I'd move to partial environments: deploy only the changed services and route the rest to the shared baseline with a routing header propagated between services (the prompt's "header routers").
  - **Monorepo note:** in a monorepo a feature group is just a branch. The cost moves to monorepo tooling (dependency graph, CI that runs only what changed, CODEOWNERS).

### D36. Strict group names, no silent conversion

- **Chose:** Group names must be lowercase letters, digits and hyphens, 1-20 characters, not starting or ending with a hyphen, and not `main`. Anything else is rejected with a clear message and a suggested rename. Names are never converted.
- **Why:** Converting (`Checkout` -> `checkout`, `cart_api` -> `cart-api`) could merge two different branch names into one group without anyone meaning it, which is exactly the accidental grouping D7 is designed against. The 20-character cap exists because group names end up in AWS resource names (ALB and target group names max out at 32).
- **Considered:** Truncating long names with a hash suffix (my first idea, in D14). Rejecting is simpler and more predictable.

### D8. A stateless reconciler decides what each env runs

- **Chose:** Every event (push, branch delete, manual run) triggers "reconcile env X". A small Python package in this repo does it, with a pure core and thin adapters:
  - **Core** (`reconciler/core.py`, no I/O): `desired_env()` maps the branches that exist to a branch-or-`main` per service (or teardown, or conflict); `resolve_images()` maps SHAs to image digests; `decide_action()` compares desired with the current stack status.
  - **Adapters:** GitHub (which branches exist), ECR (SHA -> digest), CloudFormation (which env stacks exist), CDK (`cdk deploy`).
  - **One CLI:** `python -m reconciler plan|apply`, the same command in GitHub Actions and locally. `plan --no-aws` runs without AWS credentials.
  - CDK stays declarative: it receives a resolved spec (env name + image digests) and decides nothing.
- **Why:** Edge cases fall out naturally (A's branch deleted while B's exists -> A falls back to `main`), and the prompt's scenarios become unit tests (`tests/reconciler/test_reconciler_core.py`). It must live outside the service repos because it is the only component that sees across them. Services never check each other's branches.
- **Considered:** Separate handlers per event type (push, delete, merge), which multiplies edge cases; running inside AWS (EventBridge + Lambda + CodeBuild), which is more AWS-native but more moving parts just to call `cdk deploy`.

### D26. Teardown: when no branches remain; partial deletes fall back to `main`

- **Rule:** For each registered service, use its `preview/<group>/...` branch if it has one, else `main`. The env exists only while at least one service has a matching branch.
  - A and B have branches -> A's branch + B's branch.
  - A's branch merges (or is deleted), B's remains -> **the env stays**, running A's `main` + B's branch. Once A merges, A's `main` contains A's feature, so B keeps testing against the real merged result.
  - B's branch is deleted too -> **the env is torn down.**
- **No separate state:** Desired state is read live from GitHub every run; actual state is the list of `preview-env-*` stacks in CloudFormation. Events carry no state; they only say "go look again". This is how Kubernetes controllers work.
- **"Use `main`"** means the newest `main` commit that has a built image. A merge produces a branch deletion and a push to `main` seconds apart; using the last built image avoids a gap.
- **Considered:** Tear down when *any* branch goes away (kills other services' work in progress); tear down when a "primary" branch goes away (needs extra state about who owns the group).
- **Limit (TODO, D29):** Reconciling every env when `main` moves is not built. Previews pick up newer `main` images on the next push to their group.

### D23. Previews trigger on push; branches are the source of truth

- **Chose:** Desired state comes from branches, not pull requests. Pushing to `preview/*` creates or updates an env; deleting the branch tears it down.
- **Why:** It matches the prompt ("when a feature branch is pushed"), lets developers test end to end before asking for review, keeps groups across repos simple, and is git-native.
- **Considered, PR-triggered:** Contradicts "pushed", needs draft PRs to test before review, makes cross-repo groups awkward (A has a PR, B only a branch), needs two teardown triggers that must agree, and invites a known security trap (`pull_request_target` running fork code with your cloud credentials).
- **Also rejected:** Treating "the branch's latest commit is already in `main`" as "merged". A brand-new branch with no commits looks identical, so it would never get an env. Auto-deleting branches on merge (D7) is the simpler answer.
- **Nice-to-have (TODO):** Comment the env URL on the PR when one exists.

### D24. Race conditions: one stack per group + queued deploys

- **Scenario:** A pushes `preview/checkout/api` and B pushes `preview/checkout/schema` seconds later. There must never be two envs or two ALBs.
- **Layer 1, one stack name per group:** Group `checkout` is always stack `preview-env-checkout`. A second deploy can't create a second stack; at worst CloudFormation refuses because the stack is busy.
- **Layer 2, queued deploys:** The infra repo's workflow uses a GitHub Actions `concurrency` group per env with `cancel-in-progress: false`: at most one run in progress and one waiting per env. Both service repos dispatch to the infra repo, so this serializes across repos.
- **Why dropping queued runs is safe:** GitHub keeps only the newest waiting run and cancels older waiting ones. That is only safe because every run recomputes the full desired state from the current branches (D8). A skipped run loses nothing. It also bounds the work per env however fast automation pushes.
- **Image not built yet:** If B's branch exists but its image isn't in ECR yet, the env runs B's `main` for now and the plan says so. B's dispatch after its build corrects it.
- **Shared infra:** Previews only read shared resources. The shared stack publishes its values in SSM Parameter Store, not CloudFormation exports: an export can't change while another stack imports it, which would freeze the shared stack, and SSM keeps `cdk synth` credential-free.
- **Checked and fine:** A preview copying `main` mid-migration sees either the old or the new schema, never half of it (`pg_dump` reads one consistent snapshot; migrations are transactional).
- **Not built (D29):** A cap on the number of concurrent previews (so automation creating 80 branches can't hit the ALB quota and run up costs), and routing "reconcile everything" runs through the per-env queues (one matrix job per group). The second is only needed once something reconciles everything (D21, D26).

### D25. Service registry: nothing hardcodes "A and B"

- **Chose:** `services.yaml` lists each service: name, repo, path prefix, port, health path. The CDK environment construct and the reconciler both loop over it. The GitHub owner is one setting (overridable with `PE_GITHUB_OWNER`), so a fork needs no edits.
- **Why:** Adding a service is a data change: one registry entry plus that repo's CI workflow. It is the right shape for the "35 services" discussion. Works on a personal GitHub account, no org needed: all deploys run in the infra repo, and service repos only dispatch.

---

## Compute and networking

### D4. One environment construct for `main` and every preview

- **Chose:** A single CDK construct (`infra/environment.py`) builds an env. `main` is one instance; each preview is another. The only difference is a pluggable database component (`infra/database.py`): `MainDatabase` (migrate -> seed -> app) or `PreviewDatabase` (copy-db -> migrate -> app). Both hand the app the same env vars (`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_AUTH`, ...).
- **Why:** One place to change, one place to test. Previews behave like `main` by construction, not by discipline.
- **Considered:** Separate baseline and preview stacks, which drift apart.

### D5. Compute: ECS Fargate on ARM64

- **Chose:** ECS on Fargate behind an ALB, ARM64 tasks.
- **Why:** No servers or nodes to manage, isolated tasks that start in seconds, good CDK support, and ordered startup containers (copy-db and migrate before the app, D17). ARM64 costs ~20% less and matches images built on Apple Silicon; CI builds on GitHub's ARM runners.
- **Considered:**
  - **Lambda:** each concurrent invocation opens its own Postgres connection, so bursts flood the database (usually fixed with RDS Proxy); cold starts; and the prompt describes containerized services.
  - **EKS:** namespace-per-preview with Argo CD is the standard pattern at larger scale, but a cluster takes 15-20 minutes to create, costs ~$73/month for the control plane, and is a lot to run for two services.
  - **App Runner:** less networking control and no ordered startup containers.
  - **API Gateway in front:** an ALB is simpler for container services; API Gateway adds features this doesn't need (usage plans, request transformation).
- **If this ran on EKS:** the design maps directly, and only the deploy layer of the reconciler would change.

  | Here | On EKS |
  |---|---|
  | An env's stack | A namespace per env |
  | Task definition | Pod spec |
  | copy-db and migrate containers | initContainers |
  | ALB per env | Ingress via the AWS Load Balancer Controller (IngressGroup to share one ALB) |
  | Task IAM role | EKS Pod Identity / IRSA |
  | Reconciler in GitHub Actions | Argo CD ApplicationSet with the pull-request generator |
  | Delete the stack | Delete the namespace |

### D9. Networking: private subnets and a single NAT gateway

- **Chose:** ECS tasks in private subnets with outbound traffic through one NAT gateway; ALBs in public subnets; Aurora in isolated subnets (no internet route). Task security groups accept traffic only from their env's ALB.
- **Why:** It's the standard pattern and what production would look like.
- **Revised:** First version was public subnets with no NAT ($0, tasks protected only by security groups). Public task IPs are the part a reviewer would rightly question, and cost isn't the constraint here.
- **Considered:**
  - Private subnets + VPC endpoints instead of NAT: ~$45-60/month and no internet egress at all. The locked-down option; every new outbound dependency needs an endpoint.
  - A NAT instance (e.g. fck-nat): ~$3/month.
  - One NAT per AZ: the production setup. A single NAT means an AZ failure cuts off outbound traffic, which is acceptable for dev.
- **Limits:** No API authentication and HTTP only (TLS needs a domain). Ingress is restricted by D42 instead.

### D10. Ingress: one ALB per env, path-routed

- **Chose:** Each env has its own ALB, routing `/a/*` to service A and `/b/*` to service B (from the registry, D25).
- **Why:** Isolation: one env's ALB, rules or traffic can't affect another. No rule priorities to manage, and no domain needed.
- **Costs:** ~$16-20/month per ALB, and a new ALB takes ~2-3 minutes, most of a new preview's creation time.
- **Limits:** 50 ALBs per region by default caps concurrent previews. A shared ALB hits a similar ceiling (100 rules per ALB, ~50 envs at 2 rules each). Beyond ~50 previews, raise the quota or shard.
- **Considered, a shared ALB with a rule per env:** host-based (needs a domain and wildcard DNS), path prefix per env (apps must handle `/<env>/a/...`), or header-based (easy with curl, awkward in a browser).

### D42. IP allowlisting on every ALB via a managed prefix list

- **Chose:** `preview-baseline` creates one customer-managed prefix list (`preview-baseline-alb-allowlist`) and publishes its ID to SSM. Every env's ALB security group accepts HTTP only from that list. No security group anywhere allows `0.0.0.0/0`.
  - Changing the list updates every env at once, with no redeploy.
  - **Entries stay out of CloudFormation and out of the repo.** The stack creates the list with no entries, so deploys never reset them and no IP is ever committed. `scripts/allow_ip.py add|remove|list` manages entries (default: the caller's public IP as a /32).
  - **Secure by default:** nothing is reachable until someone deliberately adds an IP.
  - CI adds its runner's IP for the smoke test (D34) and always removes it afterwards, even on failure.
- **Why:** There is no API auth (D9), and a publicly writable CRUD endpoint on the internet is wrong even for a demo. A prefix list is free, AWS-native and central.
- **Considered:** A shared-secret header checked at the ALB (plaintext over HTTP, awkward to wire into listener rules); leaving it open with an apology. Longer term: internal ALBs behind a VPN, or authentication at the ALB (needs HTTPS, so a domain).
- **Caveat:** CloudFormation leaves script-added entries alone only while the prefix list resource itself never changes. Any update to it (for example, tags) would reset the entries to the template's empty list. Its properties are deliberately fixed; if it ever happens, re-running `allow_ip.py add` restores access.

---

## Data

### D6. Each env's database starts as a copy of main's

The preview half of this decision was revised by D40 (where the database lives). The copy mechanism and the stale-branch rule below are current.

- **Chose:** `main` runs on Aurora Serverless v2 Postgres (D13) with a logical database per service (D12). When a preview env is created, a `copy-db` container copies that service's database from `main` into the preview's database, then `migrate` applies the branch's migrations on top.
- **The copy:** `copy-db` logs in to main as the service's read-only `<service>_reader` role (IAM token, SSL) and streams `pg_dump` straight into `pg_restore` (`--no-owner --no-privileges --single-transaction`). `pg_dump` reads within one transaction, so the copy is a consistent snapshot even while `main` is being written. It copies schema, data and Alembic's version table.
  - It runs **only if the preview database is empty**, i.e. on env creation. Later pushes keep the preview's data (D40).
  - Guard: `copy-db` refuses to target any database that isn't a preview database of that service, so it can never write to main's.
- **Stale branches fail loudly:** If main's database is at a migration the branch doesn't have (`main` merged a migration after the branch was cut), `migrate` fails with "main's database is at migration `<rev>`, which this branch doesn't have. Merge or rebase main into your branch." The ECS circuit breaker rolls back and the message shows in the logs. Previewing a stale branch against a newer schema tests a combination that will never reach production. Rejected: skipping migrations, which hides staleness and breaks as soon as the branch has its own.
- **Considered:** Aurora clones per preview (slow to create, right at larger data sizes); a plain Postgres container everywhere including `main` (fastest, but loses IAM auth and managed Postgres); seeding previews from scratch (D37).
- **Revised:** The first version ran the copy into a Postgres sidecar inside each preview task. D40 moved the target to a logical database on main's cluster, so data survives pushes and previews run on Aurora too.
- **Limit:** The copy slows down as main's data grows. At that point, Aurora clones (or a trimmed snapshot) are the answer.

### D37. Previews copy main's data; keeping `main` clean is main's job

- **Chose:** Keep the copy from `main` (D6). It is the literal reading of "include db replica for each".
- **The concern:** A long-lived dev database accumulates junk and previews inherit it. That is handled at the source: periodically wipe and reseed `main` (a scheduled job, or a team habit), per service if needed. Every preview created afterwards starts clean.
- **Rejected:** Seeded previews by default with an optional copy. The switch needs either per-env state outside git (plus a workflow and extra IAM) or live database swaps. Real complexity the prompt didn't ask for.
- **Principle kept:** Developers only touch git and GitHub; every AWS change goes through CI via OIDC. Operator actions such as reseeding `main` are for whoever runs the platform.

### D19. Seed data: a seed command per service; previews copy from `main`

- **Chose:** Each service repo has a small, recognizable demo dataset and a `seed` command, separate from migrations. Migrations are for schema only; demo data in a migration would eventually run in production.
- **When it runs:** In `main`, as its own container in the task: migrate -> seed -> app. It inserts only when the table is empty, so it effectively runs once. Previews don't seed; they copy main's data (D6), including anything developers added to `main` since.

### D17. Migrations: Alembic per service, run before the app starts

- **Chose:** Each service owns its Alembic migrations (D12). Migrations run in their own container before the app starts, using ECS container dependencies (`SUCCESS`): previews copy-db -> migrate -> app; `main` migrate -> seed -> app.
- **Guards:** A Postgres advisory lock so two tasks can't migrate at once; CI fails if `alembic heads` shows more than one head (two merged branches that each added a migration).
- **Team rule (documented):** Migrations on `main` must be backward compatible (expand, then contract), because old and new code briefly run side by side during a rolling deploy.
- **The payoff:** A branch's migrations only touch its own copy, so even a destructive migration can be tested safely before merge.
- **Rejected:** Migrating inside the app process at startup (replicas collide).
- **Later:** With services in several languages, I'd standardize on a language-neutral tool such as Flyway or Atlas (Atlas also lints for destructive changes in CI).

### D16. IAM database auth everywhere

- **Chose:** Each service's task role can connect only as that service's database user (`rds-db:connect` scoped to that user; per env for previews). The apps store no database passwords.
- **Why:** IAM enforces the D12 boundary alongside Postgres: service A can't even obtain a login token for B's database. No secrets to rotate.
- **With pooling:** SQLAlchemy pools connections in each app. A `do_connect` hook generates a token only when a new connection opens (signed locally, no API call). Open connections keep working after the 15-minute token expires, so IAM costs nothing per request.
- **Limits:** SSL is required, and AWS recommends keeping new IAM connections to a few hundred per second. The risk at scale is bursts of new connections; the fix is RDS Proxy (D41).

### D13. Main's database never pauses

- **Chose:** Aurora Serverless v2 at 0.5-8 ACU. The minimum of 0.5 means it never pauses. The maximum was raised from my first sizing because Aurora's connection limit scales with max ACU and every preview shares the cluster (D40, D41).
- **Considered:** Minimum 0 ACU (auto-pause). Cheapest, but the first request after idle waits ~15 seconds while the database resumes, which is the wrong first impression for anyone opening the URLs.

---

## CI/CD and security

### D11. GitHub Actions + OIDC; service repos build, the infra repo deploys

- **Chose:**
  - **Service repos** (`ci.yml`): on any push, lint (ruff), test (pytest against a real Postgres), check for a single Alembic head, and build the image. On `preview/*` or `main`, if that passes: push an ARM64 image tagged with the full commit SHA to ECR and send `repository_dispatch` to the infra repo. Deleting a `preview/*` branch also dispatches. Other branches get tests only.
  - **The infra repo** runs the reconciler for every env, including `main`: one deployer for everything. Images are resolved from commit SHA to ECR digest before deploying, never looked up by branch name.
- **Least privilege:** Each service repo's OIDC role can only push to its own ECR repository. Only the infra repo's role can deploy. A compromised service repo can't touch infra.
- **ECR lifecycle:** Keep the last 20 `main` images; expire other images after 14 days. Consequence: a preview older than that whose task gets replaced can't pull its image. Consistent with "branches shouldn't live that long"; a re-push fixes it.
- **The one real secret** is the token that lets service repos dispatch to the infra repo (a fine-grained token or a GitHub App). Everything AWS-side uses OIDC.
- **Considered:** CodePipeline/CodeBuild; reusable workflows called from each service repo (concurrency groups can't span repos, which D24 relies on).
- **Why:** No long-lived AWS credentials anywhere, deploys to one env serialized across repos, and deterministic deploys by digest.

### D32. Admin for the person deploying; CI provisions only through CDK

- **Person bootstrapping from a laptop:** Admin. `cdk bootstrap` needs broad permissions anyway, and the setup docs say so plainly.
- **The infra repo's CI role** assumes the CDK bootstrap roles, and CloudFormation (running as the bootstrap execution role) creates everything. So CI can provision anything, but only through CloudFormation stacks: consistent, auditable, reversible. It also has a few direct permissions for the reconciler: list/describe stacks, delete env stacks (D35), `ecr:DescribeImages`, `ssm:GetParameter`, and the prefix list permissions for D42.
- **Service repo roles:** push to their own ECR repository only (D11).
- **Production hardening (documented):** Narrow the CloudFormation execution role with `cdk bootstrap --cloudformation-execution-policies`. This demo keeps CDK's default (`AdministratorAccess`).

### D38. Infra repo flow: PR checks; merge to `main` deploys shared + `main`

- **Flow:**
  1. PR: ruff, pytest and `cdk synth` (no AWS credentials needed).
  2. Merge to `main`: CI deploys `preview-baseline`, then `preview-env-main`. Only this repo's `main` branch can assume the deploy role (enforced in the OIDC trust policy).
- **First-time provisioning** is done by hand with admin credentials (`cdk bootstrap`, then `preview-baseline`), because `preview-baseline` creates the OIDC roles CI uses. After that, CI takes over.
- **Existing previews** pick up changes to the environment construct on their next deploy (the next push to their group). Updating them all immediately is part of the "reconcile everything" TODO (D29).
- **TODO:** `cdk diff` on PRs, which needs a second, read-only role that PRs may assume.

### D39. Reuse an existing GitHub OIDC provider

- **Chose:** An AWS account can have only one GitHub OIDC provider. The deploy script checks IAM for one; if found, it passes a CDK context flag so `preview-baseline` reuses it (its ARN is predictable). Otherwise `preview-baseline` creates it.
- **Why in the script, not CDK:** Checking the account at synth time would break credential-free `cdk synth` (D1). Running `cdk deploy` directly without the script gives CloudFormation's unclear "already exists" error, so the flag is documented.

### D35. Teardown deletes the CloudFormation stack directly

- **Chose:** The reconciler tears down with CloudFormation `DeleteStack` and waits, not `cdk destroy`. It retries once on `DELETE_FAILED`.
- **Why:** `cdk destroy` has to synthesize the app to find the stack, and an env stack can only be synthesized from a full spec with resolved images. Teardown would then fail if an image lookup failed, and teardown is the one operation that must always work. `DeleteStack` needs no images, no synth and no Node. It's what `cdk destroy` calls underneath anyway.

---

## Reliability

### D14. Fail fast, fail loudly, fail clearly

- **Principle:** Don't try to prevent every CloudFormation failure. Make failures quick, clear and fixable by rerunning the reconciler, which is idempotent. Guard only against failures specific to this design:
  - ECS deployment circuit breaker with rollback, so CloudFormation doesn't wait up to 3 hours on tasks that never become healthy.
  - A 3-minute health-check grace period plus database connection retries in the app.
  - Stacks in `ROLLBACK_COMPLETE` (a failed create) are deleted and recreated automatically; other `*_FAILED` states are refused with a "fix by hand" message rather than guessed at.
  - Preview teardown: no deletion protection, no final snapshot, logs deleted with the stack, one retry.
  - Group names validated up front (D36), so names always fit AWS limits.
  - One queue per env (D24).
  - Config validated at app startup (for example, IAM auth requires a region and SSL).
- **Skipped (production list):** custom retry frameworks, Step Functions orchestration, drift detection.
- **Rejected:** A switch between two database strategies. It would double what has to be built and tested.

### D15. App scope: tiny CRUD plus split health checks

- **Chose:** One app, deployed twice. The code is duplicated across both repos (the prompt allows it) and differs only by config. Endpoints, all under the service's path prefix:
  - `/items`: full CRUD via SQLAlchemy.
  - `/healthz`: no database. Used by the ALB health check.
  - `/readyz`: `SELECT 1`. For smoke tests and demos.
  - `/version`: service, branch, commit and env name. No database.
- **Why the split:** If the ALB health check touched the database, a brief database blip would mark every task unhealthy and replace them all at once, turning a blip into an outage. It would also cause false rollbacks while a preview's copy is still running. Database reachability is checked on `/readyz` instead.
- **Showing it works:** Pairing is visible by calling `/a/version` and `/b/version` through one env's URL. Isolation is shown by behavior: create an item in a preview and it's absent from `main` and from other previews.
- **Revised:** An earlier `/whoami` endpoint called the peer service's `/version`. I dropped it: real services shouldn't know their peers' versions, and `/version` per service already shows the pairing. With no service-to-service calls, there's no service discovery to build either.
- **Considered:** A single read endpoint (too thin for "CRUD"); a richer domain model (effort that doesn't show infra skills); a non-Postgres store (the prompt says Postgres).

### D34. Platform smoke test after every deploy

- **Chose:** After every deploy, CI checks the env:
  - `/readyz` succeeds for every service (each database reachable).
  - `/version` on each service reports exactly the commit SHA the reconciler planned. This proves the grouping logic on every run (e.g. `checkout` really runs A's branch + B's `main`).
  - A write/read/delete round trip on `/items`.
  - Isolation: an item created in the preview doesn't appear in `main`.
- **Why:** It turns "functional end to end" from a claim into something CI checks every run, with public logs as evidence.
- **Downstream (documented):** Integration and load testing (pytest, k6, Locust) belong to the teams using the previews. The deploy job publishes the env URL as a job output so their tests can run against it.

---

## Scope and what's next

### D29. Build the must-haves; document the rest

- **Built:** The prompt's four scenarios end to end, teardown, the per-env database copy, IP allowlisting, the plan output, the smoke test, and tests for the reconciler core and the CDK templates.
- **Documented TODOs:**

  | TODO | From | Consequence until built |
  |---|---|---|
  | Nightly "reconcile all" sweep + age limit | D21 | Cleanup relies on branch-deletion events; a missed event leaves an env running until deleted by hand. |
  | Reconcile every env when `main` moves | D26 | Previews pick up newer `main` images on the next push to their group. |
  | Per-group jobs for reconcile-everything runs | D24 | Not needed until something reconciles everything. |
  | Cap on concurrent previews | D24 | Many `preview/*` branches at once could hit the ALB quota. |
  | Stale-branch warning in the plan output | D7 | Relies on "auto-delete head branches" being on. |
  | PR comment with the env URL | D7, D23 | The URL is in the job summary and job output. |
  | `cdk diff` on infra PRs | D38 | Reviewers read the synth output instead. |
  | CloudWatch alarms on the shared cluster | D40 | A cluster problem is noticed by its users. |
  | "Reset preview data" workflow | D40 | Delete and re-push the branch. |

### D21. Cleanup: nightly sweep and age limit (documented only)

- **Design:** A nightly "reconcile all" removes envs whose branches no longer exist (a backup for missed delete events). An age limit destroys previews not deployed in N days (default 7), using a `last-deployed-at` tag checked in the pure core. If the branch still exists, the next push recreates the env. Only preview stacks are ever touched; `main` never is.
- **Further options:** Idle detection from the ALB `RequestCount` metric, a "keep" label for demo envs, and an AWS Budget alarm as the last resort.

### D18. `main` is shared dev, not production (production is documented only)

- **Naming:** The env running `main` is called `main`. It is the prompt's shared dev environment.
- **Production sketch:** A separate AWS account. Keep IAM database auth and add RDS Proxy. A release tag on `main` deploys the exact image digest already tested on `main`, behind an approval: no rebuild, and no prod branch to drift.
- **Data rule:** Previews copy from `main`, never from production. Copying production data into short-lived envs is a real way customer data leaks. If realistic data were needed: an anonymized or synthetic snapshot.

### D28. Supporting infra (queues, caches) lives in each env's stack (documented only)

- **Design:** Per-env resources such as SQS queues are created and destroyed with the env's stack. A service declares them in its registry entry (e.g. `queues: [orders]`) and the environment construct creates them. A shared queue would let one preview consume another's messages.
- **Other datastores follow the same idea:** `main` gets managed services; previews get their own isolated instance, for example a Redis/Valkey container per env instead of an ElastiCache cluster each (managed instances take 10+ minutes to create). Parity is close for Valkey and rougher for OpenSearch. Fallback when a container isn't practical: one shared instance with a per-env prefix on keys or index names.
- **Why not built:** No service uses queues or caches yet.

### D30. No Docker Compose; tests use testcontainers

- **Chose:** pytest starts a throwaway Postgres with testcontainers, identically on laptops and in CI. `uv run pytest` is self-contained.
- **Why:** The showcase is the platform, not running trivial apps locally. Fewer files to maintain.
- **Considered:** A compose file per service repo; GitHub Actions service containers (CI only).

### D33. Supported OSes: macOS, Linux, Windows via WSL2

- Everything used is cross-platform (uv, Docker, Node, AWS CLI). Anything with real logic is a Python script run through uv, not bash.
- On Intel/AMD machines, local ARM64 image builds go through emulation (slow but working). CI builds on native ARM runners, so this only affects local builds.

---

## Reviewer experience

### D1. Optimize for reviewers who may never deploy it

- **Chose:** Three levels of review, each standing on its own:
  - **Read and watch:** the README, this document, the video and the public CI runs.
  - **Run locally, no AWS:** `uv run pytest`, the reconciler's `plan --no-aws`, and `cdk synth` (credential-free, because shared values come from SSM at deploy time, D24).
  - **Deploy to your own AWS account** (D20).
- **Why:** Most reviewers will read and watch. Everything checkable without AWS makes the submission more credible.

### D2. Public repos with generic names

- **Chose:** Public repos `preview-env-infra`, `service-a` and `service-b`.
- **Why:** Nothing to set up for reviewers, and Actions runs are visible to anyone. Consequences: no secrets or account-specific values in the repos (OIDC, repository variables), and a capped Aurora max ACU.

### D3. Keep the deployment running after submission

- **Chose:** `main` and at least one preview stay deployed for the review period, then everything is torn down.
- **Revised:** I originally planned open live URLs. D42 locked the ALBs down by default, so evidence comes from the video and the public CI logs (including smoke-test output). Access can be opened for a reviewer's IP on request with one command.

### D20. Bring your own AWS account

- **Chose:** Nothing account-specific in code. The account comes from the deployer's credentials; the region is a parameter, defaulting to `us-east-1`. The GitHub owner comes from the CI context (D25), so a fork works unchanged.
- **Assumption:** Whoever deploys manages their own credentials and has admin rights for first-time setup (D32).

### D27. Naming: "preview environments"

- The prompt says "ephemeral preview environments", so that's the term everywhere, including the `preview/` prefix. Also known as review apps (GitLab, Heroku) or preview deployments (Vercel, Render).

### D31. Setup docs, tested from scratch

- The setup instructions are meant to be complete and are checked by following them in a clean environment, to catch "works on my machine" gaps.
