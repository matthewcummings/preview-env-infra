"""scripts/setup_github.py: builds the right gh commands and never leaks the secret."""

from botocore.stub import Stubber
from script_fakes import FakeSession, client, stack

from scripts import setup_github

OUTPUTS = {
    "GithubOidcserviceaRoleArn55524907": "arn:aws:iam::1:role/a",
    "GithubOidcservicebRoleArnF3901FA1": "arn:aws:iam::1:role/b",
    "GithubOidcInfraRoleArnB6F6E539": "arn:aws:iam::1:role/infra",
}
TOKEN = "github_pat_SECRET_VALUE"


def var(name, repo, value):
    return ["gh", "variable", "set", name, "--repo", repo, "--body", value]


def registry_file(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(
        "github_owner: octo\nservices:\n"
        "  - {name: service-a, repo: service-a, path_prefix: /a, port: 8000, health_path: /h}\n"
        "  - {name: service-b, repo: service-b, path_prefix: /b, port: 8000, health_path: /h}\n"
    )
    return path


def run(tmp_path, monkeypatch, *, token, dry_run, runner=None):
    monkeypatch.delenv("PE_GITHUB_OWNER", raising=False)
    if token:
        monkeypatch.setenv(setup_github.TOKEN_ENV, token)
    else:
        monkeypatch.delenv(setup_github.TOKEN_ENV, raising=False)
    cfn = client("cloudformation")
    ran = []
    with Stubber(cfn) as stub:
        stub.add_response(
            "describe_stacks", stack("preview-baseline", OUTPUTS), {"StackName": "preview-baseline"}
        )
        code = setup_github.main(
            ["--dry-run"] if dry_run else [],
            session=FakeSession(cloudformation=cfn),
            runner=runner or (lambda c: ran.append(c) or 0),
            registry_path=registry_file(tmp_path),
        )
    return code, ran


def test_sets_variables_and_secret(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=TOKEN, dry_run=False)
    assert code == 0
    assert [c.args for c in ran if c.stdin is None] == [
        var("AWS_REGION", "octo/service-a", "us-east-1"),
        var("AWS_ROLE_ARN", "octo/service-a", "arn:aws:iam::1:role/a"),
        var("INFRA_REPO", "octo/service-a", "octo/preview-env-infra"),
        var("AWS_REGION", "octo/service-b", "us-east-1"),
        var("AWS_ROLE_ARN", "octo/service-b", "arn:aws:iam::1:role/b"),
        var("INFRA_REPO", "octo/service-b", "octo/preview-env-infra"),
        var("AWS_REGION", "octo/preview-env-infra", "us-east-1"),
        var("AWS_DEPLOY_ROLE_ARN", "octo/preview-env-infra", "arn:aws:iam::1:role/infra"),
    ]
    secrets = [c for c in ran if c.stdin is not None]
    assert [c.args for c in secrets] == [
        ["gh", "secret", "set", "INFRA_DISPATCH_TOKEN", "--repo", f"octo/{r}"]
        for r in ("service-a", "service-b")
    ]
    assert all(c.stdin == TOKEN for c in secrets)  # via stdin, not argv
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_dry_run_runs_nothing(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=TOKEN, dry_run=True)
    out = capsys.readouterr().out
    assert code == 0
    assert ran == []
    assert "(dry run) $ gh secret set INFRA_DISPATCH_TOKEN --repo octo/service-a" in out
    assert TOKEN not in out


def test_missing_token_explains_how_to_create_it(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=None, dry_run=False)
    out = capsys.readouterr().out
    assert code == 1
    assert len(ran) == 8  # variables still set
    assert "personal-access-tokens/new" in out
    assert "Contents: Read and write" in out


def test_gh_failure_stops(tmp_path, monkeypatch, capsys):
    code, _ = run(tmp_path, monkeypatch, token=TOKEN, dry_run=False, runner=lambda c: 1)
    assert code == 1
    assert "gh auth login" in capsys.readouterr().err
