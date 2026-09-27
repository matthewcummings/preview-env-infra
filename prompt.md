# The take-home prompt

The assignment as given, verbatim. Greeting and submission instructions omitted.

---

Your team runs multiple containerized microservices with a shared database. Every developer works on features which may span one or more of these microservices. When multiple developers have multiple feature branches across microservices, testing each “feature group” independently becomes critical before that “feature group” is marked as ready for production. However, right now, there is only a single shared dev environment, meaning that testing is done sequentially for each “feature group” rather than in parallel, causing conflicts and/or deployment delays. Your job is to fix that.

Build an AWS CDK project that:

- Defines baseline infrastructure — Set up 2 repos, each containing a basic FastAPI Postgres CRUD application with a Dockerfile (you can duplicate the code across both repos). Define the baseline infrastructure to run these containerized microservices on AWS.
- Automates ephemeral preview environments — When a feature branch is pushed to GitHub for either of these repos, a parallel, isolated copy of the infrastructure spins up automatically. For example,
  - If repo A has a new branch and repo B does not, spin up repo A with the new branch and repo B with the main branch in this “new A ephemeral environment”
  - If repo B has a new branch and repo A does not, spin up repo B with the new branch and repo A with the main branch in this “new B ephemeral environment”
  - If repo A has a new branch and repo B also has a new branch
    - If both branches are part of the same feature group, spin up repo A with the new branch and repo B with the new branch in this “new A new B ephemeral environment”. Feature groups can be linked through tags / branch names / header routers etc - your call!
    - If both branches are not part of the same feature group, spin up both “new A ephemeral environment” and “new B ephemeral environment”

Your ephemeral environments should be functional end-to-end (i.e.: include db replica for each). When the feature branch is deleted or merged, the environment tears down.

You have full autonomy over how you handle compute, database engine, networking, data seeding and replication strategy, and CI/CD tooling. This is by design because instead of being prescriptive, we want to see how you think through problems, make decisions and balance trade-offs.
