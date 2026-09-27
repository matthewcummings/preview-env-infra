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


COMMENT_TOKEN = "github_pat_COMMENT_SECRET"


def run(tmp_path, monkeypatch, *, token, dry_run, runner=None, comment_token=COMMENT_TOKEN):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    for name, value in (
        (setup_github.TOKEN_ENV, token),
        (setup_github.COMMENT_TOKEN_ENV, comment_token),
    ):
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
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


def test_sets_variables_and_secrets(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=TOKEN, dry_run=False)
    assert code == 0
    assert [c.args for c in ran if c.stdin is None] == [
        ["gh", "repo", "edit", "octo/service-a", "--delete-branch-on-merge"],
        ["gh", "repo", "edit", "octo/service-b", "--delete-branch-on-merge"],
        ["gh", "repo", "edit", "octo/preview-env-infra", "--delete-branch-on-merge"],
        var("AWS_REGION", "octo/service-a", "us-east-1"),
        var("AWS_ROLE_ARN", "octo/service-a", "arn:aws:iam::1:role/a"),
        var("INFRA_REPO", "octo/service-a", "octo/preview-env-infra"),
        var("AWS_REGION", "octo/service-b", "us-east-1"),
        var("AWS_ROLE_ARN", "octo/service-b", "arn:aws:iam::1:role/b"),
        var("INFRA_REPO", "octo/service-b", "octo/preview-env-infra"),
        var("AWS_REGION", "octo/preview-env-infra", "us-east-1"),
        var("AWS_DEPLOY_ROLE_ARN", "octo/preview-env-infra", "arn:aws:iam::1:role/infra"),
    ]
    secrets = [(c.args, c.stdin) for c in ran if c.stdin is not None]
    assert secrets == [
        (["gh", "secret", "set", "INFRA_DISPATCH_TOKEN", "--repo", "octo/service-a"], TOKEN),
        (["gh", "secret", "set", "INFRA_DISPATCH_TOKEN", "--repo", "octo/service-b"], TOKEN),
        (
            ["gh", "secret", "set", "PREVIEW_COMMENT_TOKEN", "--repo", "octo/preview-env-infra"],
            COMMENT_TOKEN,
        ),
    ]  # values via stdin, not argv
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert COMMENT_TOKEN not in captured.out + captured.err


def test_dry_run_runs_nothing(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=TOKEN, dry_run=True)
    out = capsys.readouterr().out
    assert code == 0
    assert ran == []
    assert (
        "(dry run) $ gh secret set INFRA_DISPATCH_TOKEN --repo octo/service-a"
        "  (value from $INFRA_DISPATCH_TOKEN)"
    ) in out
    assert (
        "(dry run) $ gh secret set PREVIEW_COMMENT_TOKEN --repo octo/preview-env-infra"
        "  (value from $PREVIEW_COMMENT_TOKEN)"
    ) in out
    for repo in ("service-a", "service-b", "preview-env-infra"):
        assert f"(dry run) $ gh repo edit octo/{repo} --delete-branch-on-merge" in out
    assert TOKEN not in out and COMMENT_TOKEN not in out


def help_part(out):
    return out.split("personal-access-tokens/new", 1)[1]


def test_missing_dispatch_token_explains_only_that_one(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=None, dry_run=False)
    out = capsys.readouterr().out
    assert code == 1
    assert len(ran) == 12  # everything else (incl. the comment token secret) still set
    assert "Not set: INFRA_DISPATCH_TOKEN." in out
    # Least privilege: only the infra repo, only Contents read/write, short expiry.
    assert 'Repository access: "Only select repositories": ONLY octo/preview-env-infra' in out
    assert "Contents: Read and write (needed to send repository_dispatch)" in out
    assert "Expiration: 30 days" in out
    assert "infra workflow's own GITHUB_TOKEN" in out
    assert "PREVIEW_COMMENT_TOKEN:" not in out
    assert "service-a" not in help_part(out)


def test_missing_comment_token_explains_only_that_one(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=TOKEN, comment_token=None, dry_run=False)
    out = capsys.readouterr().out
    assert code == 1
    assert len(ran) == 13  # variables + the dispatch token secrets still set
    assert "Not set: PREVIEW_COMMENT_TOKEN." in out
    assert (
        'Repository access: "Only select repositories": ONLY octo/service-a, octo/service-b'
    ) in out
    assert "Repository permissions: Pull requests: Read and write." in out
    assert "INFRA_DISPATCH_TOKEN:" not in out
    assert "Contents" not in help_part(out)


def test_both_tokens_missing_explains_both(tmp_path, monkeypatch, capsys):
    code, ran = run(tmp_path, monkeypatch, token=None, comment_token=None, dry_run=False)
    out = capsys.readouterr().out
    assert code == 1
    assert len(ran) == 11
    assert "Not set: INFRA_DISPATCH_TOKEN, PREVIEW_COMMENT_TOKEN." in out
    assert "INFRA_DISPATCH_TOKEN: the service repos' CI uses it only to signal" in out
    assert "PREVIEW_COMMENT_TOKEN: octo/preview-env-infra's reconcile workflow uses it only" in out


def test_gh_failure_stops(tmp_path, monkeypatch, capsys):
    code, _ = run(tmp_path, monkeypatch, token=TOKEN, dry_run=False, runner=lambda c: 1)
    assert code == 1
    assert "gh auth login" in capsys.readouterr().err
