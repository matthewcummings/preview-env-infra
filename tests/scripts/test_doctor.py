"""scripts/doctor.py with fake tools, a fake `gh`, a fake HTTP POST and stubbed AWS.

No network, no credentials, no real gh.
"""

import json

import pytest
from botocore.stub import Stubber
from script_fakes import FakeSession, client, stack

from reconciler.registry import Registry, Service
from scripts import doctor
from scripts.doctor import Status

ARN = "arn:aws:iam::123456789012:user/me"
ALL_TOOLS = {"uv", "docker", "node", "npx", "aws", "gh"}
REGISTRY = Registry(
    github_owner="octo",
    services=(
        Service("service-a", "service-a", "/a", 8000, "/healthz"),
        Service("service-b", "service-b", "/b", 8000, "/healthz"),
    ),
)
REPOS = ("octo/service-a", "octo/service-b", "octo/preview-env-infra")
SERVICE_VARS = ["AWS_REGION", "AWS_ROLE_ARN", "INFRA_REPO"]
INFRA_VARS = ["AWS_REGION", "AWS_DEPLOY_ROLE_ARN"]


def which(present):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


class FakeGh:
    """Answers the gh commands doctor runs from an in-memory picture of the repos."""

    def __init__(self, *, logged_in=True, missing_repos=(), auto_delete=True, configured=True):
        self.logged_in = logged_in
        self.repos = {r: {"auto_delete": auto_delete} for r in REPOS if r not in missing_repos}
        self.variables = {
            r: (INFRA_VARS if r.endswith("infra") else SERVICE_VARS) if configured else []
            for r in REPOS
        }
        self.secrets = {
            r: (["INFRA_DISPATCH_TOKEN"] if configured and not r.endswith("infra") else [])
            for r in REPOS
        }
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        match args:
            case ["auth", "status"]:
                return (0, "Logged in") if self.logged_in else (1, "You are not logged in\n")
            case ["api", path, "--jq", ".delete_branch_on_merge"]:
                repo = path.removeprefix("repos/")
                if repo not in self.repos:
                    return 1, "gh: Not Found (HTTP 404)\n"
                return 0, "true\n" if self.repos[repo]["auto_delete"] else "false\n"
            case [("variable" | "secret") as kind, "list", "--repo", repo, "--json", "name"]:
                names = (self.variables if kind == "variable" else self.secrets)[repo]
                return 0, json.dumps([{"name": n} for n in names])
        raise AssertionError(f"unexpected gh call {args}")


def never_post(url, token, body):
    raise AssertionError("must not POST without a token")


def github(gh=None, *, tools=ALL_TOOLS | {"gh"}, token=None, post=never_post):
    return doctor.check_github(
        REGISTRY,
        infra_repo="preview-env-infra",
        which=which(tools),
        gh=gh or FakeGh(),
        post=post,
        token=token,
    )


def by_name(results):
    return {r.name: r for r in results}


# --- AWS and tools (end to end through main) -----------------------------------------------


def registry_file(tmp_path, owner="octo"):
    path = tmp_path / "services.yaml"
    path.write_text(f"github_owner: {owner}\nservices: []\n")
    return path


def run(tmp_path, monkeypatch, *, tools=ALL_TOOLS, owner="octo", region="us-east-1"):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    monkeypatch.delenv("INFRA_DISPATCH_TOKEN", raising=False)
    sts, cfn = client("sts"), client("cloudformation")
    with Stubber(sts) as sts_stub, Stubber(cfn) as cfn_stub:
        sts_stub.add_response(
            "get_caller_identity", {"UserId": "u", "Account": "123456789012", "Arn": ARN}
        )
        cfn_stub.add_response("describe_stacks", stack("CDKToolkit"))
        return doctor.main(
            [],
            session=FakeSession(region, sts=sts, cloudformation=cfn),
            which=which(tools),
            gh=FakeGh(),
            post=never_post,
            registry_path=registry_file(tmp_path, owner),
        )


def test_all_good(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert f"[PASS] AWS credentials: {ARN} (123456789012)" in out
    assert "[PASS] CDK bootstrap: CDKToolkit in us-east-1: UPDATE_COMPLETE" in out
    assert "[PASS] gh: installed and logged in" in out
    assert "All required checks passed (0 warning(s))." in out


def test_missing_tool_fails_with_install_hint(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, tools=ALL_TOOLS - {"npx"}) == 1
    out = capsys.readouterr().out
    assert "[FAIL] npx: not found" in out
    assert "-> install: comes with Node.js" in out


def test_placeholder_owner_fails_and_skips_github(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, owner="CHANGE_ME") == 1
    out = capsys.readouterr().out
    assert "export PREVIEW_ENV_GITHUB_OWNER" in out
    assert "[SKIP] GitHub repos: GitHub owner not set" in out


def test_region_unset_is_only_a_warning(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, region=None) == 0
    assert "[WARN] region: not set; the tools default to us-east-1" in capsys.readouterr().out


def test_not_bootstrapped(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("INFRA_DISPATCH_TOKEN", raising=False)
    sts, cfn = client("sts"), client("cloudformation")
    with Stubber(sts) as sts_stub, Stubber(cfn) as cfn_stub:
        sts_stub.add_response(
            "get_caller_identity", {"UserId": "u", "Account": "123456789012", "Arn": ARN}
        )
        cfn_stub.add_client_error(
            "describe_stacks", "ValidationError", "Stack CDKToolkit does not exist"
        )
        code = doctor.main(
            [],
            session=FakeSession(sts=sts, cloudformation=cfn),
            which=which(ALL_TOOLS),
            gh=FakeGh(),
            post=never_post,
            registry_path=registry_file(tmp_path),
        )
    # Expected before `make bootstrap` (README step 5): a warning, not a failure.
    assert code == 0
    out = capsys.readouterr().out
    assert "[WARN] CDK bootstrap: CDKToolkit not found" in out
    assert "run `make bootstrap`" in out


def test_bad_credentials_skip_the_bootstrap_check(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("INFRA_DISPATCH_TOKEN", raising=False)
    sts = client("sts")
    with Stubber(sts) as stub:
        stub.add_client_error("get_caller_identity", "ExpiredToken", "token expired")
        code = doctor.main(
            [],
            session=FakeSession(sts=sts),  # no cloudformation client: must not be needed
            which=which(ALL_TOOLS),
            gh=FakeGh(),
            post=never_post,
            registry_path=registry_file(tmp_path),
        )
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] AWS credentials" in out
    assert "[SKIP] CDK bootstrap: needs working AWS credentials" in out


# --- GitHub checks ---------------------------------------------------------------------------


def test_github_fully_configured_passes():
    results = github()
    assert all(r.status in (Status.PASS, Status.SKIP) for r in results), results
    names = by_name(results)
    for repo in REPOS:
        assert names[f"repo {repo}"].status is Status.PASS
        assert names[f"{repo} auto-delete head branches"].status is Status.PASS
        assert names[f"{repo} variables"].status is Status.PASS
    assert names["octo/service-a secret"].status is Status.PASS
    assert "octo/preview-env-infra secret" not in names  # the secret lives on service repos


def test_gh_missing_fails_and_skips_repo_checks():
    names = by_name(github(tools=ALL_TOOLS - {"gh"}))
    assert names["gh"].status is Status.FAIL
    assert names["GitHub repos"].status is Status.SKIP


def test_gh_not_logged_in_fails():
    names = by_name(github(FakeGh(logged_in=False)))
    assert names["gh"].status is Status.FAIL
    assert names["gh"].hint == "run `gh auth login`"
    assert names["GitHub repos"].status is Status.SKIP


def test_missing_repo_fails_and_skips_its_other_checks():
    names = by_name(github(FakeGh(missing_repos=("octo/service-b",))))
    assert names["repo octo/service-b"].status is Status.FAIL
    assert "Not Found" in names["repo octo/service-b"].detail
    assert "octo/service-b variables" not in names
    assert names["repo octo/service-a"].status is Status.PASS


def test_fresh_setup_is_warnings_not_failures():
    # Repos exist, but `make setup-github` hasn't run: no auto-delete, vars or secret.
    results = github(FakeGh(auto_delete=False, configured=False))
    assert not any(r.status is Status.FAIL for r in results)
    names = by_name(results)
    assert names["octo/service-a auto-delete head branches"].status is Status.WARN
    assert (
        names["octo/service-a variables"].detail == "missing AWS_REGION, AWS_ROLE_ARN, INFRA_REPO"
    )
    assert names["octo/preview-env-infra variables"].detail == (
        "missing AWS_REGION, AWS_DEPLOY_ROLE_ARN"
    )
    assert names["octo/service-a secret"].detail == "missing INFRA_DISPATCH_TOKEN"
    assert all(r.hint == "run `make setup-github`" for r in results if r.status is Status.WARN)


def test_partially_missing_variable_is_named():
    gh = FakeGh()
    gh.variables["octo/service-b"] = ["AWS_REGION", "INFRA_REPO"]
    names = by_name(github(gh))
    assert names["octo/service-b variables"].status is Status.WARN
    assert names["octo/service-b variables"].detail == "missing AWS_ROLE_ARN"


# --- Dispatch token --------------------------------------------------------------------------


def test_dispatch_skipped_without_token():
    result = by_name(github())["INFRA_DISPATCH_TOKEN can dispatch to octo/preview-env-infra"]
    assert result.status is Status.SKIP
    assert "only needed while running `make setup-github`" in result.detail


def test_dispatch_204_passes_and_uses_this_token():
    posts = []

    def post(url, token, body):
        posts.append((url, token, body))
        return 204

    result = doctor.check_dispatch_token("octo/preview-env-infra", "tok", post)
    assert result.status is Status.PASS
    assert posts == [
        (
            "https://api.github.com/repos/octo/preview-env-infra/dispatches",
            "tok",
            {"event_type": "doctor-permission-check"},
        )
    ]


@pytest.mark.parametrize(
    ("status", "words"), [(403, "Contents: Read and write"), (404, "can't see")]
)
def test_dispatch_403_404_fail_with_pointer_to_instructions(status, words):
    result = doctor.check_dispatch_token("octo/preview-env-infra", "tok", lambda *a: status)
    assert result.status is Status.FAIL
    assert words in result.detail
    assert "token instructions printed by `make setup-github`" in result.hint


def test_dispatch_unreachable_is_a_warning():
    result = doctor.check_dispatch_token("octo/preview-env-infra", "tok", lambda *a: 0)
    assert result.status is Status.WARN


def test_dispatch_failure_makes_doctor_exit_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INFRA_DISPATCH_TOKEN", "github_pat_SECRET_VALUE")
    sts, cfn = client("sts"), client("cloudformation")
    with Stubber(sts) as sts_stub, Stubber(cfn) as cfn_stub:
        sts_stub.add_response(
            "get_caller_identity", {"UserId": "u", "Account": "123456789012", "Arn": ARN}
        )
        cfn_stub.add_response("describe_stacks", stack("CDKToolkit"))
        code = doctor.main(
            [],
            session=FakeSession(sts=sts, cloudformation=cfn),
            which=which(ALL_TOOLS | {"gh"}),
            gh=FakeGh(),
            post=lambda *a: 403,
            registry_path=registry_file(tmp_path),
        )
    out = capsys.readouterr().out
    assert code == 1
    assert "SECRET_VALUE" not in out  # the token value is never printed
    assert "[FAIL] INFRA_DISPATCH_TOKEN can dispatch to octo/preview-env-infra" in out


def test_docker_missing_is_only_a_warning(tmp_path, monkeypatch, capsys):
    """Deploying doesn't need Docker; only the service repos' tests do."""
    assert run(tmp_path, monkeypatch, tools=ALL_TOOLS - {"docker"}) == 0
    assert "[WARN] docker: not found" in capsys.readouterr().out
