"""Fakes shared by the script tests: stubbed boto3 clients behind a fake session."""

import boto3

NOW = "2026-09-26T00:00:00Z"


def client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


class FakeSession:
    """Stands in for boto3.Session: hands out pre-built (stubbed) clients by service name."""

    def __init__(self, region_name: str | None = "us-east-1", **clients) -> None:
        self.region_name = region_name
        self.clients = clients

    def client(self, name: str, region_name: str | None = None):
        return self.clients[name]


def stack(name: str, outputs: dict[str, str] | None = None, status: str = "UPDATE_COMPLETE"):
    return {
        "Stacks": [
            {
                "StackName": name,
                "StackStatus": status,
                "CreationTime": NOW,
                "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in (outputs or {}).items()],
            }
        ]
    }
