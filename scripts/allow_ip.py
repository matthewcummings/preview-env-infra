"""Manage the ALB allowlist (D42): the prefix list every env's ALB accepts HTTP from.

    uv run python scripts/allow_ip.py list
    uv run python scripts/allow_ip.py add    [--cidr 203.0.113.7/32] [--description "..."]
    uv run python scripts/allow_ip.py remove [--cidr 203.0.113.7/32]

Without --cidr, uses the caller's current public IPv4 address as a /32. The prefix list ID
comes from SSM (published by pe-shared), so this works against whichever account/region
your AWS credentials point at. Changes apply to every env immediately, no redeploy.

Idempotent: adding a CIDR that's already there, or removing one that isn't, does nothing
and says so. Safe to run concurrently (e.g. two CI jobs): every change is conditioned on
the list's current version, and a lost race is retried.
"""

import argparse
import getpass
import ipaddress
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

if __package__ in (None, ""):
    # Run as a file (`python scripts/allow_ip.py`): make the repo root importable so the SSM
    # key comes from the same place pe-shared publishes it (no copy to drift).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION, SsmKeys  # noqa: E402

CHECKIP_URL = "https://checkip.amazonaws.com"
MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 2.0
SETTLE_TIMEOUT_SECONDS = 60.0

# Lost a race with another writer: the list's version moved on, or a modification is
# still in progress. Both are safe to retry after re-reading the version.
RETRYABLE_ERRORS = {"PrefixListVersionMismatch", "IncorrectState"}
SETTLED_STATES = {"create-complete", "modify-complete", "restore-complete"}
FAILED_STATES = {"create-failed", "modify-failed", "restore-failed"}


class AllowlistError(Exception):
    pass


def normalize_cidr(value: str) -> str:
    """'203.0.113.7' -> '203.0.113.7/32'. IPv4 only (the list is IPv4)."""
    try:
        network = ipaddress.ip_network(value if "/" in value else f"{value}/32", strict=True)
    except ValueError as e:
        raise AllowlistError(f"not a valid CIDR: {value!r} ({e})") from None
    if network.version != 4:
        raise AllowlistError(f"the allowlist is IPv4 only, got {value!r}")
    return str(network)


def current_public_ip(fetch: Callable[[str], str] | None = None) -> str:
    fetch = fetch or _http_get
    try:
        ip = fetch(CHECKIP_URL).strip()
    except OSError as e:
        raise AllowlistError(f"couldn't get your public IP from {CHECKIP_URL}: {e}") from None
    return normalize_cidr(ip)


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (fixed https URL)
        return response.read().decode()


class Allowlist:
    def __init__(self, ec2, prefix_list_id: str, sleep: Callable[[float], None] = time.sleep):
        self.ec2 = ec2
        self.prefix_list_id = prefix_list_id
        self.sleep = sleep

    @classmethod
    def from_ssm(cls, ssm, ec2, **kwargs) -> Allowlist:
        param = ssm.get_parameter(Name=SsmKeys.ALB_ALLOWLIST_PREFIX_LIST_ID)
        return cls(ec2, param["Parameter"]["Value"], **kwargs)

    def entries(self) -> dict[str, str]:
        """CIDR -> description."""
        result: dict[str, str] = {}
        paginator = self.ec2.get_paginator("get_managed_prefix_list_entries")
        for page in paginator.paginate(PrefixListId=self.prefix_list_id):
            for entry in page.get("Entries", []):
                result[entry["Cidr"]] = entry.get("Description", "")
        return result

    def add(self, cidr: str, description: str) -> bool:
        """Returns False (no-op) if `cidr` is already allowed."""
        return self._modify(cidr, present=True, description=description)

    def remove(self, cidr: str) -> bool:
        """Returns False (no-op) if `cidr` isn't in the list."""
        return self._modify(cidr, present=False)

    def _modify(self, cidr: str, *, present: bool, description: str = "") -> bool:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            version = self._settled_version()
            if (cidr in self.entries()) == present:
                return False
            change = (
                {"AddEntries": [{"Cidr": cidr, "Description": description[:255]}]}
                if present
                else {"RemoveEntries": [{"Cidr": cidr}]}
            )
            try:
                self.ec2.modify_managed_prefix_list(
                    PrefixListId=self.prefix_list_id, CurrentVersion=version, **change
                )
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code not in RETRYABLE_ERRORS or attempt == MAX_ATTEMPTS:
                    raise
                print(f"  prefix list changed concurrently ({code}); retrying ({attempt})")
                self.sleep(RETRY_DELAY_SECONDS)
                continue
            self._settled_version()  # wait until the change is in effect
            return True
        raise AssertionError("unreachable")

    def _settled_version(self) -> int:
        """The list's current version, once no modification is in progress."""
        waited = 0.0
        while True:
            (pl,) = self.ec2.describe_managed_prefix_lists(PrefixListIds=[self.prefix_list_id])[
                "PrefixLists"
            ]
            state = pl["State"]
            if state in SETTLED_STATES:
                return pl["Version"]
            if state in FAILED_STATES:
                raise AllowlistError(
                    f"prefix list {self.prefix_list_id} is in state {state}: "
                    f"{pl.get('StateMessage', 'no details')}"
                )
            if waited >= SETTLE_TIMEOUT_SECONDS:
                raise AllowlistError(f"prefix list {self.prefix_list_id} stuck in state {state}")
            self.sleep(RETRY_DELAY_SECONDS)
            waited += RETRY_DELAY_SECONDS


def default_description() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        repo = os.environ.get("GITHUB_REPOSITORY", "?")
        return f"GitHub Actions {repo} run {os.environ.get('GITHUB_RUN_ID', '?')}"
    return f"{getpass.getuser()} via allow_ip.py"


def main(argv: list[str] | None = None, *, session=None, fetch=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["add", "remove", "list"])
    parser.add_argument("--cidr", help="IPv4 address or CIDR (default: your public IP /32)")
    parser.add_argument("--description", help="entry description (add only)")
    args = parser.parse_args(argv)

    session = session or boto3.Session()
    region = session.region_name or DEFAULT_REGION
    allowlist = Allowlist.from_ssm(
        session.client("ssm", region_name=region), session.client("ec2", region_name=region)
    )
    try:
        if args.action == "list":
            entries = allowlist.entries()
            print(f"{allowlist.prefix_list_id} ({region}): {len(entries)} entries")
            for cidr, description in sorted(entries.items()):
                print(f"  {cidr:<20} {description}")
            return 0

        cidr = normalize_cidr(args.cidr) if args.cidr else current_public_ip(fetch)
        if args.action == "add":
            changed = allowlist.add(cidr, args.description or default_description())
            print(
                f"added {cidr} to {allowlist.prefix_list_id}"
                if changed
                else f"{cidr} is already in {allowlist.prefix_list_id}; nothing to do"
            )
        else:
            changed = allowlist.remove(cidr)
            print(
                f"removed {cidr} from {allowlist.prefix_list_id}"
                if changed
                else f"{cidr} is not in {allowlist.prefix_list_id}; nothing to do"
            )
        return 0
    except AllowlistError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
