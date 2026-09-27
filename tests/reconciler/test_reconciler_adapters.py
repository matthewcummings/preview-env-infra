"""Adapters against canned responses: a fake urllib opener and botocore's Stubber."""

import io
import json
import urllib.error
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from reconciler.adapters.aws import CloudFormationStacks, EcrImages
from reconciler.adapters.cdk import CdkRunner
from reconciler.adapters.github import GitHubBranches
from reconciler.core import Branch
from reconciler.ports import ReconcileError

SHA = "a" * 40
DIGEST = "sha256:" + "d" * 64


# --- GitHub -----------------------------------------------------------------------------


class FakeOpener:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.requests = body, error, []

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.body).encode())


def test_github_branches_uses_matching_refs():
    opener = FakeOpener(
        [{"ref": "refs/heads/preview/checkout/api", "object": {"sha": SHA, "type": "commit"}}]
    )
    gh = GitHubBranches("acme", token="t0k", opener=opener)

    assert gh.branches("service-a", "preview/checkout") == [Branch("preview/checkout/api", SHA)]
    request = opener.requests[0]
    assert request.full_url == (
        "https://api.github.com/repos/acme/service-a/git/matching-refs/heads/preview/checkout"
    )
    assert request.get_header("Authorization") == "Bearer t0k"


def test_github_main_commits():
    opener = FakeOpener([{"sha": SHA}, {"sha": "b" * 40}])
    gh = GitHubBranches("acme", token="", opener=opener)
    assert gh.main_commits("service-a", 20) == [SHA, "b" * 40]
    assert opener.requests[0].full_url.endswith(
        "/repos/acme/service-a/commits?sha=main&per_page=20"
    )
    assert opener.requests[0].get_header("Authorization") is None


def test_github_404_explains_owner_setting():
    error = urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)
    gh = GitHubBranches("nobody", token="", opener=FakeOpener(error=error))
    with pytest.raises(ReconcileError, match="Check github_owner in services.yaml"):
        gh.branches("service-a", "preview/x")


def test_github_rate_limit_suggests_token():
    error = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
    gh = GitHubBranches("acme", token="", opener=FakeOpener(error=error))
    with pytest.raises(ReconcileError, match="set GITHUB_TOKEN"):
        gh.main_commits("service-a", 1)


# --- ECR / CloudFormation ----------------------------------------------------------------


@pytest.fixture
def aws_env(monkeypatch):
    # Stubbed clients never call AWS, but botocore still wants credentials to sign.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


def test_ecr_image_found_is_pinned_by_digest(aws_env):
    client = boto3.client("ecr", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_response(
            "describe_images",
            {"imageDetails": [{"registryId": "123456789012", "imageDigest": DIGEST}]},
            {"repositoryName": "service-a", "imageIds": [{"imageTag": SHA}]},
        )
        uri = EcrImages(client, "us-east-1").image_for("service-a", SHA)
    assert uri == f"123456789012.dkr.ecr.us-east-1.amazonaws.com/service-a@{DIGEST}"


def test_ecr_image_missing_is_none(aws_env):
    client = boto3.client("ecr", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_client_error("describe_images", "ImageNotFoundException")
        assert EcrImages(client, "us-east-1").image_for("service-a", SHA) is None


def test_ecr_repo_missing_is_a_clear_error(aws_env):
    client = boto3.client("ecr", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_client_error("describe_images", "RepositoryNotFoundException")
        with pytest.raises(ReconcileError, match="pe-shared"):
            EcrImages(client, "us-east-1").image_for("service-a", SHA)


def test_cloudformation_lists_env_stacks_only(aws_env):
    client = boto3.client("cloudformation", region_name="us-east-1")
    now = "2026-09-26T00:00:00Z"
    with Stubber(client) as stub:
        stub.add_response(
            "list_stacks",
            {
                "StackSummaries": [
                    {
                        "StackName": "pe-env-checkout",
                        "StackStatus": "ROLLBACK_COMPLETE",
                        "CreationTime": now,
                    },
                    {
                        "StackName": "pe-env-main",
                        "StackStatus": "UPDATE_COMPLETE",
                        "CreationTime": now,
                    },
                    {
                        "StackName": "pe-shared",
                        "StackStatus": "UPDATE_COMPLETE",
                        "CreationTime": now,
                    },
                ]
            },
        )
        stacks = CloudFormationStacks(client).env_stacks()
    assert stacks == {"checkout": "ROLLBACK_COMPLETE", "main": "UPDATE_COMPLETE"}


# --- CDK runner ---------------------------------------------------------------------------


def test_cdk_deploy_command():
    calls = []
    cdk = CdkRunner(cwd=Path("/repo"), runner=lambda cmd, cwd: calls.append((cmd, cwd)) or 0)
    cdk.deploy("checkout", Path("/tmp/spec.json"))
    assert calls == [
        (
            [
                "npx",
                "cdk",
                "deploy",
                "pe-env-checkout",
                "-c",
                "envSpec=/tmp/spec.json",
                "--require-approval",
                "never",
            ],
            Path("/repo"),
        ),
    ]
    assert not hasattr(cdk, "destroy")  # teardown is CloudFormation DeleteStack (D35)


def test_cdk_failure_raises():
    cdk = CdkRunner(runner=lambda cmd, cwd: 1)
    with pytest.raises(ReconcileError, match="exit code 1"):
        cdk.deploy("checkout", Path("/tmp/spec.json"))


# --- CloudFormation delete (D35) ----------------------------------------------------------

STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/pe-env-checkout/abc"
NOW = "2026-09-26T00:00:00Z"


def stack(status):
    return {
        "Stacks": [
            {
                "StackId": STACK_ID,
                "StackName": "pe-env-checkout",
                "StackStatus": status,
                "CreationTime": NOW,
            }
        ]
    }


def events(*items):
    return {
        "StackEvents": [
            {
                "EventId": eid,
                "StackId": STACK_ID,
                "StackName": "pe-env-checkout",
                "Timestamp": NOW,
                "LogicalResourceId": rid,
                "ResourceStatus": status,
                **({"ResourceStatusReason": reason} if reason else {}),
            }
            for eid, rid, status, reason in reversed(items)  # API: newest first
        ]
    }


OLD = ("e0", "pe-env-checkout", "UPDATE_COMPLETE", None)
BY_ID = {"StackName": STACK_ID}


def deleter(client, logs):
    return CloudFormationStacks(client, log=logs.append, poll_seconds=0, polls_per_round=1)


def test_delete_waits_and_prints_new_events(aws_env):
    client = boto3.client("cloudformation", region_name="us-east-1")
    logs = []
    with Stubber(client) as stub:
        stub.add_response(
            "describe_stacks", stack("UPDATE_COMPLETE"), {"StackName": "pe-env-checkout"}
        )
        stub.add_response("describe_stack_events", events(OLD), BY_ID)
        stub.add_response("delete_stack", {}, BY_ID)
        # Round 1: still in progress (the waiter's single poll, then our status check).
        stub.add_response("describe_stacks", stack("DELETE_IN_PROGRESS"), BY_ID)
        stub.add_response("describe_stacks", stack("DELETE_IN_PROGRESS"), BY_ID)
        stub.add_response(
            "describe_stack_events",
            events(OLD, ("e1", "Alb", "DELETE_IN_PROGRESS", None)),
            BY_ID,
        )
        # Round 2: done.
        stub.add_response("describe_stacks", stack("DELETE_COMPLETE"), BY_ID)
        stub.add_response(
            "describe_stack_events",
            events(
                OLD,
                ("e1", "Alb", "DELETE_IN_PROGRESS", None),
                ("e2", "Alb", "DELETE_COMPLETE", None),
            ),
            BY_ID,
        )
        deleter(client, logs).delete("checkout")
        stub.assert_no_pending_responses()
    assert logs == [
        "Deleting stack pe-env-checkout (attempt 1 of 2)...",
        "  Alb DELETE_IN_PROGRESS",
        "  ...still deleting (DELETE_IN_PROGRESS)",
        "  Alb DELETE_COMPLETE",
        "Stack pe-env-checkout deleted.",
    ]


def test_delete_retries_once_then_fails_loudly(aws_env):
    client = boto3.client("cloudformation", region_name="us-east-1")
    logs = []
    failed = ("e1", "PreviewDb", "DELETE_FAILED", "Data API timeout")
    failed_again = ("e2", "PreviewDb", "DELETE_FAILED", "Data API timeout again")
    with Stubber(client) as stub:
        stub.add_response("describe_stacks", stack("DELETE_FAILED"))
        stub.add_response("describe_stack_events", events(OLD))
        for new_events in ([OLD, failed], [OLD, failed, failed_again]):
            stub.add_response("delete_stack", {})
            stub.add_response("describe_stacks", stack("DELETE_FAILED"))  # waiter: failure
            stub.add_response("describe_stacks", stack("DELETE_FAILED"))  # our status check
            stub.add_response("describe_stack_events", events(*new_events))
        with pytest.raises(ReconcileError) as exc:
            deleter(client, logs).delete("checkout")
        stub.assert_no_pending_responses()
    message = str(exc.value)
    assert (
        "could not delete stack pe-env-checkout: it is DELETE_FAILED after 2 attempt(s)" in message
    )
    assert "PreviewDb: Data API timeout again" in message
    assert "Stack pe-env-checkout is DELETE_FAILED; retrying the delete once." in logs


def test_delete_missing_stack_is_a_noop(aws_env):
    client = boto3.client("cloudformation", region_name="us-east-1")
    logs = []
    with Stubber(client) as stub:
        stub.add_client_error(
            "describe_stacks", "ValidationError", "Stack with id pe-env-checkout does not exist"
        )
        deleter(client, logs).delete("checkout")
    assert logs == ["Stack pe-env-checkout does not exist; nothing to delete."]
