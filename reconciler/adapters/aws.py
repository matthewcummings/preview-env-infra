"""AWS adapters (boto3): ECR image lookups, the env stack inventory, and env teardown.

The only AWS changes made here are env stack deletes (D35). Creates and updates go
through `cdk deploy` (adapters/cdk.py).
"""

from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError, WaiterError

from reconciler.core import STACK_PREFIX, stack_name
from reconciler.ports import ReconcileError

# Every status except DELETE_COMPLETE: a deleted stack is the same as no stack.
LIVE_STACK_STATUSES = [
    "CREATE_IN_PROGRESS",
    "CREATE_FAILED",
    "CREATE_COMPLETE",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_FAILED",
    "ROLLBACK_COMPLETE",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_COMPLETE",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE",
    "REVIEW_IN_PROGRESS",
    "IMPORT_IN_PROGRESS",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_IN_PROGRESS",
    "IMPORT_ROLLBACK_FAILED",
    "IMPORT_ROLLBACK_COMPLETE",
]


class EcrImages:
    """Image tag = full commit SHA (contract "Images"); resolved to a digest so CDK pins it."""

    def __init__(self, client: Any, region: str) -> None:
        self.client = client
        self.region = region

    def image_for(self, repository: str, sha: str) -> str | None:
        try:
            response = self.client.describe_images(
                repositoryName=repository, imageIds=[{"imageTag": sha}]
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ImageNotFoundException":
                return None
            if code == "RepositoryNotFoundException":
                raise ReconcileError(
                    f"ECR repository '{repository}' not found in {self.region}. "
                    "Is the preview-baseline stack deployed in this account and region?"
                ) from err
            raise
        detail = response["imageDetails"][0]
        registry = f"{detail['registryId']}.dkr.ecr.{self.region}.amazonaws.com"
        return f"{registry}/{repository}@{detail['imageDigest']}"


class CloudFormationStacks:
    """Lists env stacks and deletes them.

    A delete is DeleteStack + the `stack_delete_complete` waiter. The waiter runs in short
    rounds so the new stack events can be printed in between: a teardown that takes
    minutes should show progress in the CI log, not silence.
    """

    DELETE_ATTEMPTS = 2  # the first try plus one retry for DELETE_FAILED

    def __init__(
        self,
        client: Any,
        *,
        log: Callable[[str], None] = print,
        poll_seconds: int = 10,
        polls_per_round: int = 6,
        max_rounds: int = 60,  # 60 rounds x 6 polls x 10 s = 1 hour
    ) -> None:
        self.client = client
        self.log = log
        self.poll_seconds = poll_seconds
        self.polls_per_round = polls_per_round
        self.max_rounds = max_rounds

    def env_stacks(self) -> dict[str, str]:
        stacks: dict[str, str] = {}
        paginator = self.client.get_paginator("list_stacks")
        for page in paginator.paginate(StackStatusFilter=LIVE_STACK_STATUSES):
            for summary in page["StackSummaries"]:
                name = summary["StackName"]
                if name.startswith(STACK_PREFIX):
                    stacks[name.removeprefix(STACK_PREFIX)] = summary["StackStatus"]
        return stacks

    def outputs(self, env: str) -> dict[str, str]:
        response = self.client.describe_stacks(StackName=f"{STACK_PREFIX}{env}")
        outputs = response["Stacks"][0].get("Outputs", [])
        return {o["OutputKey"]: o["OutputValue"] for o in outputs}

    def delete(self, env: str) -> None:
        name = stack_name(env)
        stack = self._describe(name)
        if stack is None:
            self.log(f"Stack {name} does not exist; nothing to delete.")
            return
        # Use the stack ID from here on: once deleted, the name no longer resolves, but the
        # ID still returns the stack (DELETE_COMPLETE) and its events.
        stack_id = stack["StackId"]
        seen = {e["EventId"] for e in self._events(stack_id)}  # only print new events

        for attempt in range(1, self.DELETE_ATTEMPTS + 1):
            self.log(f"Deleting stack {name} (attempt {attempt} of {self.DELETE_ATTEMPTS})...")
            self.client.delete_stack(StackName=stack_id)
            status, failures = self._wait_for_delete(stack_id, seen)
            if status == "DELETE_COMPLETE":
                self.log(f"Stack {name} deleted.")
                return
            if status == "DELETE_FAILED" and attempt < self.DELETE_ATTEMPTS:
                self.log(f"Stack {name} is DELETE_FAILED; retrying the delete once.")
                continue
            details = "".join(f"\n  - {f}" for f in failures) or " (no failure events found)"
            raise ReconcileError(
                f"could not delete stack {name}: it is {status} after {attempt} attempt(s). "
                f"Resources that failed to delete:{details}\n"
                "Fix or remove those resources by hand (CloudFormation console or CLI), "
                "then rerun the reconciler."
            )

    def _wait_for_delete(self, stack_id: str, seen: set[str]) -> tuple[str, list[str]]:
        """Wait until the delete finishes. Returns (final status, delete failure reasons)."""
        waiter = self.client.get_waiter("stack_delete_complete")
        failures: list[str] = []
        status = "DELETE_IN_PROGRESS"
        for _ in range(self.max_rounds):
            try:
                waiter.wait(
                    StackName=stack_id,
                    WaiterConfig={"Delay": self.poll_seconds, "MaxAttempts": self.polls_per_round},
                )
                status = "DELETE_COMPLETE"
            except WaiterError:
                # Either this round ran out of polls, or the delete failed. The status says which.
                stack = self._describe(stack_id)
                status = stack["StackStatus"] if stack else "DELETE_COMPLETE"
            failures += self._print_new_events(stack_id, seen)
            if status != "DELETE_IN_PROGRESS":
                return status, failures
            self.log(f"  ...still deleting ({status})")
        return f"{status} (timed out waiting)", failures

    def _print_new_events(self, stack_id: str, seen: set[str]) -> list[str]:
        failures = []
        for event in reversed(self._events(stack_id)):  # the API returns newest first
            if event["EventId"] in seen:
                continue
            seen.add(event["EventId"])
            reason = event.get("ResourceStatusReason", "")
            line = f"{event['LogicalResourceId']} {event['ResourceStatus']}"
            self.log(f"  {line}{f': {reason}' if reason else ''}")
            if event["ResourceStatus"] == "DELETE_FAILED":
                failures.append(f"{event['LogicalResourceId']}: {reason or 'no reason given'}")
        return failures

    def _describe(self, name_or_id: str) -> dict[str, Any] | None:
        try:
            return self.client.describe_stacks(StackName=name_or_id)["Stacks"][0]
        except ClientError as err:
            if "does not exist" in err.response.get("Error", {}).get("Message", ""):
                return None
            raise

    def _events(self, stack_id: str) -> list[dict[str, Any]]:
        # The newest page (up to 100 events) is plenty for one delete's progress.
        return self.client.describe_stack_events(StackName=stack_id)["StackEvents"]
