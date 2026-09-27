"""scripts/doctor.py with fake tools and stubbed AWS: no network, no credentials."""

from botocore.stub import Stubber
from script_fakes import FakeSession, client, stack

from scripts import doctor

ARN = "arn:aws:iam::123456789012:user/me"
ALL_TOOLS = {"uv", "docker", "node", "npx", "aws"}


def which(present):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


def registry_file(tmp_path, owner="octo"):
    path = tmp_path / "services.yaml"
    path.write_text(f"github_owner: {owner}\nservices: []\n")
    return path


def aws_ok(sts_stub, cfn_stub, status="UPDATE_COMPLETE"):
    sts_stub.add_response(
        "get_caller_identity",
        {"UserId": "u", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"},
    )
    cfn_stub.add_response("describe_stacks", stack("CDKToolkit", status=status))


def run(
    tmp_path,
    monkeypatch,
    *,
    tools=ALL_TOOLS,
    owner="octo",
    status="UPDATE_COMPLETE",
    region="us-east-1",
):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    sts, cfn = client("sts"), client("cloudformation")
    with Stubber(sts) as sts_stub, Stubber(cfn) as cfn_stub:
        aws_ok(sts_stub, cfn_stub, status)
        return doctor.main(
            [],
            session=FakeSession(region, sts=sts, cloudformation=cfn),
            which=which(tools),
            registry_path=registry_file(tmp_path, owner),
        )


def test_all_good(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "[ok  ] AWS credentials: arn:aws:iam::123456789012:user/me (123456789012)" in out
    assert "[ok  ] CDK bootstrap: CDKToolkit in us-east-1: UPDATE_COMPLETE" in out
    assert "All good." in out


def test_missing_tool_fails_with_install_hint(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, tools=ALL_TOOLS - {"npx"}) == 1
    out = capsys.readouterr().out
    assert "[FAIL] npx: not found" in out
    assert "-> install: comes with Node.js" in out


def test_placeholder_owner_fails(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, owner="CHANGE_ME") == 1
    assert "export PREVIEW_ENV_GITHUB_OWNER" in capsys.readouterr().out


def test_region_unset_is_only_a_warning(tmp_path, monkeypatch, capsys):
    assert run(tmp_path, monkeypatch, region=None) == 0
    assert "[warn] region: not set; the tools default to us-east-1" in capsys.readouterr().out


def test_not_bootstrapped(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
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
            registry_path=registry_file(tmp_path),
        )
    assert code == 1
    assert "run `make bootstrap`" in capsys.readouterr().out


def test_bad_credentials_skip_the_bootstrap_check(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    sts = client("sts")
    with Stubber(sts) as stub:
        stub.add_client_error("get_caller_identity", "ExpiredToken", "token expired")
        code = doctor.main(
            [],
            session=FakeSession(sts=sts),  # no cloudformation client: must not be needed
            which=which(ALL_TOOLS),
            registry_path=registry_file(tmp_path),
        )
    assert code == 1
    assert "[FAIL] AWS credentials" in capsys.readouterr().out
