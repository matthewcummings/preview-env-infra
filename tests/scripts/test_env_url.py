"""scripts/env_url.py with a stubbed CloudFormation client."""

from botocore.stub import Stubber
from script_fakes import FakeSession, client, stack

from scripts import env_url


def test_prints_the_url(capsys):
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response(
            "describe_stacks",
            stack("preview-env-checkout", {"EnvUrl1E3EC4AF": "http://alb.example.com/"}),
            {"StackName": "preview-env-checkout"},
        )
        code = env_url.main(["checkout"], session=FakeSession(cloudformation=cfn))
    assert code == 0
    assert capsys.readouterr().out == "http://alb.example.com\n"


def test_missing_env_is_a_clear_error(capsys):
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_client_error(
            "describe_stacks", "ValidationError", "Stack with id preview-env-x does not exist"
        )
        code = env_url.main(["x"], session=FakeSession(cloudformation=cfn))
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "is preview-env-x deployed?" in captured.err
