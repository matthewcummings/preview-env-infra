"""scripts/deploy_baseline.py: OIDC provider reuse (D39) with stubbed AWS, no network."""

import pytest
from botocore.stub import Stubber
from script_fakes import FakeSession, client

from scripts import deploy_baseline

PROVIDER_ARN = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"


def resources(stub: Stubber, *types: str) -> None:
    stub.add_response(
        "list_stack_resources",
        {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": f"R{i}",
                    "ResourceType": t,
                    "LastUpdatedTimestamp": "2026-09-26T00:00:00Z",
                    "ResourceStatus": "CREATE_COMPLETE",
                }
                for i, t in enumerate(types)
            ]
        },
        {"StackName": "preview-baseline"},
    )


def no_stack(stub: Stubber) -> None:
    stub.add_client_error(
        "list_stack_resources", "ValidationError", "Stack with id preview-baseline does not exist"
    )


def providers(stub: Stubber, *arns: str) -> None:
    stub.add_response(
        "list_open_id_connect_providers", {"OpenIDConnectProviderList": [{"Arn": a} for a in arns]}
    )


def no_iam():
    raise AssertionError("IAM must not be called when preview-baseline already exists")


def test_existing_stack_that_owns_the_provider_keeps_creating_it():
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        resources(stub, "AWS::EC2::VPC", "AWS::IAM::OIDCProvider")
        assert deploy_baseline.choose_oidc_mode(cfn, no_iam)[0] == "create"


def test_existing_stack_that_imported_the_provider_keeps_importing_it():
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        resources(stub, "AWS::EC2::VPC", "AWS::IAM::Role")
        assert deploy_baseline.choose_oidc_mode(cfn, no_iam)[0] == "existing"


@pytest.mark.parametrize(
    ("arns", "mode"),
    [
        ([PROVIDER_ARN], "existing"),
        (["arn:aws:iam::123456789012:oidc-provider/other.example.com"], "create"),
        ([], "create"),
    ],
)
def test_first_deploy_checks_the_account(arns, mode):
    cfn, iam = client("cloudformation"), client("iam")
    with Stubber(cfn) as cfn_stub, Stubber(iam) as iam_stub:
        no_stack(cfn_stub)
        providers(iam_stub, *arns)
        assert deploy_baseline.choose_oidc_mode(cfn, lambda: iam)[0] == mode


def test_cdk_command_passes_the_context_flag_only_for_existing():
    base = ["npx", "cdk", "deploy", "preview-baseline", "--require-approval", "never"]
    assert deploy_baseline.cdk_deploy_command("create") == base
    assert deploy_baseline.cdk_deploy_command("existing") == [
        *base,
        "-c",
        "githubOidcProvider=existing",
    ]


def test_main_deploys_then_allows_my_ip(capsys):
    cfn, iam = client("cloudformation"), client("iam")
    commands, allowed = [], []
    with Stubber(cfn) as cfn_stub, Stubber(iam) as iam_stub:
        no_stack(cfn_stub)
        providers(iam_stub, PROVIDER_ARN)
        code = deploy_baseline.main(
            ["--allow-my-ip"],
            session=FakeSession(cloudformation=cfn, iam=iam),
            runner=lambda cmd: commands.append(cmd) or 0,
            allow=lambda argv: allowed.append(argv) or 0,
        )
    assert code == 0
    assert commands == [deploy_baseline.cdk_deploy_command("existing")]
    assert allowed == [["add"]]
    assert "GitHub OIDC provider: existing" in capsys.readouterr().out


def test_main_stops_when_cdk_fails(capsys):
    cfn = client("cloudformation")
    allowed = []
    with Stubber(cfn) as stub:
        resources(stub, "AWS::IAM::OIDCProvider")
        code = deploy_baseline.main(
            ["--allow-my-ip"],
            session=FakeSession(cloudformation=cfn),
            runner=lambda cmd: 1,
            allow=lambda argv: allowed.append(argv) or 0,
        )
    assert code == 1
    assert allowed == []
    assert "cdk deploy preview-baseline failed" in capsys.readouterr().err
