"""scripts/allow_ip.py against stubbed AWS clients (botocore Stubber): no network, no creds."""

import boto3
import pytest
from botocore.stub import Stubber

from infra.config import SsmKeys
from scripts import allow_ip
from scripts.allow_ip import Allowlist, AllowlistError, normalize_cidr

PL = "pl-0123456789abcdef0"
CIDR = "203.0.113.7/32"


def client(service: str):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def ec2():
    ec2 = client("ec2")
    with Stubber(ec2) as stubber:
        yield ec2, stubber
        stubber.assert_no_pending_responses()


def described(stubber: Stubber, version: int, state: str = "modify-complete") -> None:
    stubber.add_response(
        "describe_managed_prefix_lists",
        {"PrefixLists": [{"PrefixListId": PL, "Version": version, "State": state}]},
        {"PrefixListIds": [PL]},
    )


def entries(stubber: Stubber, *cidrs: str) -> None:
    stubber.add_response(
        "get_managed_prefix_list_entries",
        {"Entries": [{"Cidr": c, "Description": f"desc {c}"} for c in cidrs]},
        {"PrefixListId": PL},
    )


def no_sleep(_seconds: float) -> None:
    pass


def test_add_new_cidr_then_waits_for_the_change(ec2):
    ec2, stubber = ec2
    described(stubber, 3)
    entries(stubber)
    stubber.add_response(
        "modify_managed_prefix_list",
        {},
        {
            "PrefixListId": PL,
            "CurrentVersion": 3,
            "AddEntries": [{"Cidr": CIDR, "Description": "me"}],
        },
    )
    described(stubber, 3, state="modify-in-progress")
    described(stubber, 4)
    assert Allowlist(ec2, PL, sleep=no_sleep).add(CIDR, "me") is True


def test_add_existing_cidr_is_a_noop(ec2):
    ec2, stubber = ec2
    described(stubber, 3)
    entries(stubber, CIDR)
    assert Allowlist(ec2, PL, sleep=no_sleep).add(CIDR, "me") is False


def test_remove_existing_cidr(ec2):
    ec2, stubber = ec2
    described(stubber, 5)
    entries(stubber, CIDR, "198.51.100.0/24")
    stubber.add_response(
        "modify_managed_prefix_list",
        {},
        {"PrefixListId": PL, "CurrentVersion": 5, "RemoveEntries": [{"Cidr": CIDR}]},
    )
    described(stubber, 6)
    assert Allowlist(ec2, PL, sleep=no_sleep).remove(CIDR) is True


def test_remove_missing_cidr_is_a_noop(ec2):
    ec2, stubber = ec2
    described(stubber, 5)
    entries(stubber, "198.51.100.0/24")
    assert Allowlist(ec2, PL, sleep=no_sleep).remove(CIDR) is False


def test_lost_race_is_retried_with_the_new_version(ec2):
    ec2, stubber = ec2
    described(stubber, 3)
    entries(stubber)
    stubber.add_client_error(
        "modify_managed_prefix_list", service_error_code="PrefixListVersionMismatch"
    )
    # Retry: another writer bumped the version to 4 (and added its own entry).
    described(stubber, 4)
    entries(stubber, "198.51.100.9/32")
    stubber.add_response(
        "modify_managed_prefix_list",
        {},
        {
            "PrefixListId": PL,
            "CurrentVersion": 4,
            "AddEntries": [{"Cidr": CIDR, "Description": "me"}],
        },
    )
    described(stubber, 5)
    assert Allowlist(ec2, PL, sleep=no_sleep).add(CIDR, "me") is True


def test_other_errors_are_not_retried(ec2):
    ec2, stubber = ec2
    described(stubber, 3)
    entries(stubber)
    stubber.add_client_error(
        "modify_managed_prefix_list", service_error_code="UnauthorizedOperation"
    )
    with pytest.raises(Exception, match="UnauthorizedOperation"):
        Allowlist(ec2, PL, sleep=no_sleep).add(CIDR, "me")


def test_failed_list_state_is_reported(ec2):
    ec2, stubber = ec2
    described(stubber, 3, state="modify-failed")
    with pytest.raises(AllowlistError, match="modify-failed"):
        Allowlist(ec2, PL, sleep=no_sleep).add(CIDR, "me")


def test_prefix_list_id_comes_from_ssm(ec2):
    ec2, _ = ec2
    ssm = client("ssm")
    with Stubber(ssm) as stubber:
        stubber.add_response(
            "get_parameter",
            {"Parameter": {"Name": SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID, "Value": PL}},
            {"Name": "/pe/shared/alb-allowlist-prefix-list-id"},
        )
        assert Allowlist.from_ssm(ssm, ec2).prefix_list_id == PL


@pytest.mark.parametrize(
    ("value", "expected"),
    [("203.0.113.7", CIDR), (CIDR, CIDR), ("198.51.100.0/24", "198.51.100.0/24")],
)
def test_normalize_cidr(value, expected):
    assert normalize_cidr(value) == expected


@pytest.mark.parametrize("value", ["nope", "198.51.100.1/24", "2001:db8::1", "0.0.0.0/33"])
def test_normalize_cidr_rejects_bad_input(value):
    with pytest.raises(AllowlistError):
        normalize_cidr(value)


def test_default_cidr_is_the_callers_public_ip():
    assert allow_ip.current_public_ip(lambda _url: "203.0.113.7\n") == CIDR


def test_public_ip_lookup_failure_is_a_clear_error():
    def fail(_url):
        raise OSError("no network")

    with pytest.raises(AllowlistError, match="public IP"):
        allow_ip.current_public_ip(fail)


class FakeSession:
    """Just enough of boto3.Session for main(): hands out pre-stubbed clients."""

    region_name = "us-east-1"

    def __init__(self, clients):
        self.clients = clients

    def client(self, service, region_name=None):
        return self.clients[service]


def test_main_add_uses_public_ip_and_prints_a_noop(capsys):
    ssm, ec2 = client("ssm"), client("ec2")
    with Stubber(ssm) as ssm_stub, Stubber(ec2) as ec2_stub:
        ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Value": PL}},
            {"Name": SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID},
        )
        described(ec2_stub, 1)
        entries(ec2_stub, CIDR)
        code = allow_ip.main(
            ["add"],
            session=FakeSession({"ssm": ssm, "ec2": ec2}),
            fetch=lambda _url: "203.0.113.7",
        )
    assert code == 0
    assert "already in" in capsys.readouterr().out


def test_main_list(capsys):
    ssm, ec2 = client("ssm"), client("ec2")
    with Stubber(ssm) as ssm_stub, Stubber(ec2) as ec2_stub:
        ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Value": PL}},
            {"Name": SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID},
        )
        entries(ec2_stub, CIDR)
        code = allow_ip.main(["list"], session=FakeSession({"ssm": ssm, "ec2": ec2}))
    out = capsys.readouterr().out
    assert code == 0
    assert "1 entries" in out and CIDR in out


def test_main_rejects_a_bad_cidr(capsys):
    ssm = client("ssm")
    with Stubber(ssm) as ssm_stub:
        ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Value": PL}},
            {"Name": SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID},
        )
        code = allow_ip.main(
            ["add", "--cidr", "nope"], session=FakeSession({"ssm": ssm, "ec2": client("ec2")})
        )
    assert code == 1
    assert "not a valid CIDR" in capsys.readouterr().err
