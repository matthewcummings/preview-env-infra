"""Preflight checklist (D14 `make doctor`): tools, AWS access, CDK bootstrap, GitHub wiring.

    uv run python scripts/doctor.py

Read-only, apart from one harmless call: if INFRA_DISPATCH_TOKEN is set locally, it sends a
repository_dispatch of type "doctor-permission-check" to the infra repo to prove the token
can. No workflow listens for that type, so no run starts.

Levels: PASS, WARN (works, or just means `make setup-github` hasn't run yet), FAIL (must
fix), SKIP (not checked). Exit code 1 only if something FAILs.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, Registry, load_registry  # noqa: E402
from scripts._common import infra_repo_name  # noqa: E402

BOOTSTRAP_STACK = "CDKToolkit"
PLACEHOLDER_OWNER = "CHANGE_ME"
OWNER_ENV = "PREVIEW_ENV_GITHUB_OWNER"
TOKEN_ENV = "INFRA_DISPATCH_TOKEN"
COMMENT_TOKEN_ENV = "PREVIEW_COMMENT_TOKEN"
TOKEN_DOCS = "see the token instructions printed by `make setup-github`"
DISPATCH_CHECK_EVENT = "doctor-permission-check"
SETUP_HINT = "run `make setup-github`"

SERVICE_REPO_VARIABLES = ("AWS_REGION", "AWS_ROLE_ARN", "INFRA_REPO")
INFRA_REPO_VARIABLES = ("AWS_REGION", "AWS_DEPLOY_ROLE_ARN")

# (tool, install hint, required). gh is checked with the GitHub checks below.
TOOLS = [
    ("uv", "https://docs.astral.sh/uv/getting-started/installation/", True),
    ("node", "https://nodejs.org/ (Node 24; the CDK CLI runs on it)", True),
    ("npx", "comes with Node.js", True),
    ("aws", "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html", True),
    # Deploying doesn't need Docker (CI builds the images); only the service repos' tests do.
    ("docker", "https://docs.docker.com/get-docker/ (only for the service repos' tests)", False),
]

# gh(args) -> (exit code, stdout if it succeeded else stderr)
type Gh = Callable[[list[str]], tuple[int, str]]
# post(url, token, json body) -> HTTP status (0 if unreachable)
type Post = Callable[[str, str, dict[str, Any]], int]
# get(url, token) -> HTTP status (0 if unreachable)
type Get = Callable[[str, str], int]


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Result:
    status: Status
    name: str
    detail: str
    hint: str = ""


def check_tools(which: Callable[[str], str | None]) -> list[Result]:
    results = []
    for tool, hint, required in TOOLS:
        path = which(tool)
        if path:
            results.append(Result(Status.PASS, tool, path))
        else:
            status = Status.FAIL if required else Status.WARN
            results.append(Result(status, tool, "not found", f"install: {hint}"))
    return results


def check_aws(session: Any) -> list[Result]:
    from botocore.exceptions import BotoCoreError, ClientError

    results = []
    region = session.region_name
    results.append(
        Result(Status.PASS, "region", region)
        if region
        else Result(
            Status.WARN,
            "region",
            f"not set; the tools default to {DEFAULT_REGION}",
            "set AWS_REGION (or `aws configure set region <region>`) to choose one",
        )
    )
    region = region or DEFAULT_REGION

    try:
        identity = session.client("sts", region_name=region).get_caller_identity()
    except (BotoCoreError, ClientError) as err:
        results.append(
            Result(
                Status.FAIL, "AWS credentials", str(err), "run `aws configure` or `aws sso login`"
            )
        )
        results.append(Result(Status.SKIP, "CDK bootstrap", "needs working AWS credentials"))
        return results
    results.append(
        Result(Status.PASS, "AWS credentials", f"{identity['Arn']} ({identity['Account']})")
    )

    cfn = session.client("cloudformation", region_name=region)
    hint = f"run `make bootstrap` (npx cdk bootstrap) for {region}"
    try:
        (stack,) = cfn.describe_stacks(StackName=BOOTSTRAP_STACK)["Stacks"]
    except ClientError as err:
        # Expected on a fresh account before step 5 of the README, so a warning, not a failure.
        # (A bootstrap stack that exists but is broken is still a FAIL, below.)
        results.append(
            Result(Status.WARN, "CDK bootstrap", f"{BOOTSTRAP_STACK} not found: {err}", hint)
        )
        return results
    status = stack["StackStatus"]
    ok = status.endswith("_COMPLETE") and "ROLLBACK" not in status
    results.append(
        Result(
            Status.PASS if ok else Status.FAIL,
            "CDK bootstrap",
            f"{BOOTSTRAP_STACK} in {region}: {status}",
            "" if ok else hint,
        )
    )
    return results


def check_github_owner(registry: Registry) -> Result:
    if registry.github_owner == PLACEHOLDER_OWNER:
        return Result(
            Status.FAIL,
            "GitHub owner",
            f"github_owner is still {PLACEHOLDER_OWNER}",
            f"export {OWNER_ENV}=<your GitHub user or org>, or edit services.yaml",
        )
    source = OWNER_ENV if os.environ.get(OWNER_ENV) else "services.yaml"
    return Result(Status.PASS, "GitHub owner", f"{registry.github_owner} (from {source})")


def check_github(
    registry: Registry,
    *,
    infra_repo: str,
    which: Callable[[str], str | None],
    gh: Gh,
    post: Post,
    token: str | None,
    get: Get | None = None,
    comment_token: str | None = None,
) -> list[Result]:
    owner = registry.github_owner
    if owner == PLACEHOLDER_OWNER:
        return [Result(Status.SKIP, "GitHub repos", "GitHub owner not set (see above)")]

    infra_full = f"{owner}/{infra_repo}"
    service_repos = [f"{owner}/{s.repo}" for s in registry.services]
    results = [
        check_dispatch_token(infra_full, token, post),
        *check_comment_token(service_repos, comment_token, get or http_get),
    ]

    # gh is required: `make setup-github` configures the repos with it, and these checks use it.
    if which("gh") is None:
        return [
            Result(Status.FAIL, "gh", "not found", "install https://cli.github.com"),
            Result(Status.SKIP, "GitHub repos", "needs gh"),
            *results,
        ]
    code, out = gh(["auth", "status"])
    if code != 0:
        return [
            Result(Status.FAIL, "gh", f"not logged in: {_first_line(out)}", "run `gh auth login`"),
            Result(Status.SKIP, "GitHub repos", "needs `gh auth login`"),
            *results,
        ]
    repo_results = [Result(Status.PASS, "gh", "installed and logged in")]

    # Each secret lives where its user runs: the dispatch token in the service repos (their
    # CI signals the infra repo), the comment token in the infra repo (it comments on PRs).
    repos = [(r, SERVICE_REPO_VARIABLES, [TOKEN_ENV]) for r in service_repos]
    repos.append((infra_full, INFRA_REPO_VARIABLES, [COMMENT_TOKEN_ENV]))
    for repo, variables, secrets in repos:
        repo_results += check_repo(repo, variables, secrets=secrets, gh=gh)
    return repo_results + results


def check_repo(
    repo: str, variables: Sequence[str], *, secrets: Sequence[str], gh: Gh
) -> list[Result]:
    code, out = gh(["api", f"repos/{repo}", "--jq", ".delete_branch_on_merge"])
    if code != 0:
        return [
            Result(
                Status.FAIL,
                f"repo {repo}",
                f"not found or not visible: {_first_line(out)}",
                f"create or fork it, or check {OWNER_ENV} / services.yaml",
            )
        ]
    results = [Result(Status.PASS, f"repo {repo}", "exists")]

    if out.strip() == "true":
        results.append(Result(Status.PASS, f"{repo} auto-delete head branches", "on"))
    else:
        results.append(Result(Status.WARN, f"{repo} auto-delete head branches", "off", SETUP_HINT))

    results.append(_names_present(gh, repo, "variable", "variables", variables))
    if secrets:
        results.append(_names_present(gh, repo, "secret", "secret", secrets))
    return results


def _names_present(gh: Gh, repo: str, kind: str, label: str, wanted: Sequence[str]) -> Result:
    """Are these Actions variables/secrets set? Names only; secret values are never readable."""
    name = f"{repo} {label}"
    code, out = gh([kind, "list", "--repo", repo, "--json", "name"])
    if code != 0:
        return Result(Status.WARN, name, f"couldn't list: {_first_line(out)}", SETUP_HINT)
    try:
        present = {entry["name"] for entry in json.loads(out or "[]")}
    except ValueError, KeyError, TypeError:
        return Result(Status.WARN, name, f"unexpected `gh {kind} list` output", SETUP_HINT)
    missing = [w for w in wanted if w not in present]
    if missing:
        return Result(Status.WARN, name, f"missing {', '.join(missing)}", SETUP_HINT)
    return Result(Status.PASS, name, ", ".join(wanted))


def check_dispatch_token(infra_full: str, token: str | None, post: Post) -> Result:
    """Prove the local token can send repository_dispatch to the infra repo (what the
    service repos' CI does with it). Uses the token itself, not gh's login."""
    name = f"{TOKEN_ENV} can dispatch to {infra_full}"
    if not token:
        return Result(
            Status.SKIP,
            name,
            f"{TOKEN_ENV} not set locally (only needed while running `make setup-github`)",
        )
    url = f"https://api.github.com/repos/{infra_full}/dispatches"
    status = post(url, token, {"event_type": DISPATCH_CHECK_EVENT})
    if status == 204:
        return Result(Status.PASS, name, "yes (204)")
    hint = f"the token needs access to ONLY that repo with Contents: Read and write; {TOKEN_DOCS}"
    reasons = {
        401: "token rejected (401): expired or mistyped",
        403: "forbidden (403): the token lacks Contents: Read and write",
        404: "not found (404): the token can't see that repo",
    }
    if status in reasons:
        return Result(Status.FAIL, name, reasons[status], hint)
    return Result(Status.WARN, name, f"unexpected response ({status or 'unreachable'})", hint)


def check_comment_token(repos: Sequence[str], token: str | None, get: Get) -> list[Result]:
    """Can the local comment token read each service repo's PRs? Read only: proving write
    access would mean posting a comment, so that part isn't checked."""
    if not token:
        return [
            Result(
                Status.SKIP,
                f"{COMMENT_TOKEN_ENV} can access the service repos",
                f"{COMMENT_TOKEN_ENV} not set locally (only needed while running "
                "`make setup-github`)",
            )
        ]
    hint = (
        f"the token needs access to ONLY the service repos with Pull requests: Read and "
        f"write; {TOKEN_DOCS}"
    )
    reasons = {
        401: "token rejected (401): expired or mistyped",
        403: "forbidden (403): the token lacks Pull requests access",
        404: "not found (404): the token can't see this repo",
    }
    results = []
    for repo in repos:
        name = f"{COMMENT_TOKEN_ENV} can access {repo}"
        status = get(f"https://api.github.com/repos/{repo}/pulls?per_page=1", token)
        if status == 200:
            results.append(
                Result(
                    Status.PASS,
                    name,
                    f"can access {repo}; write access can't be checked without side effects",
                )
            )
        elif status in reasons:
            results.append(Result(Status.FAIL, name, reasons[status], hint))
        else:
            results.append(
                Result(Status.WARN, name, f"unexpected response ({status or 'unreachable'})", hint)
            )
    return results


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or ["no output"])[0]


def run_gh(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def http_get(url: str, token: str) -> int:
    request = urllib.request.Request(url, headers=_github_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except urllib.error.URLError, TimeoutError:
        return 0


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "preview-env-doctor",
    }


def http_post(url: str, token: str, body: dict[str, Any]) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers=_github_headers(token),
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except urllib.error.URLError, TimeoutError:
        return 0


def render(results: Sequence[Result]) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.status}] {r.name}: {r.detail}")
        if r.hint and r.status in (Status.WARN, Status.FAIL):
            lines.append(f"       -> {r.hint}")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any = None,
    which: Callable[[str], str | None] = shutil.which,
    gh: Gh = run_gh,
    post: Post = http_post,
    get: Get = http_get,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    if session is None:
        import boto3

        session = boto3.Session()
    registry = load_registry(registry_path)
    results = [
        *check_tools(which),
        *check_aws(session),
        check_github_owner(registry),
        *check_github(
            registry,
            infra_repo=infra_repo_name(),
            which=which,
            gh=gh,
            post=post,
            token=os.environ.get(TOKEN_ENV) or None,
            get=get,
            comment_token=os.environ.get(COMMENT_TOKEN_ENV) or None,
        ),
    ]
    print(render(results))
    failed = sum(r.status is Status.FAIL for r in results)
    warned = sum(r.status is Status.WARN for r in results)
    if failed:
        print(f"\n{failed} problem(s) to fix first, {warned} warning(s).")
    else:
        print(f"\nAll required checks passed ({warned} warning(s)).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
