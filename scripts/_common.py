"""Small helpers shared by the scripts in this folder."""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# CDK names a CfnOutput after its construct path plus an 8-character hash, e.g. the env
# URL output `EnvUrl1E3EC4AF`. Strip the hash so scripts can use the stable part.
_CDK_HASH = re.compile(r"[0-9A-F]{8}$")

type Runner = Callable[[Sequence[str]], int]


def cdk_output_id(*construct_ids: str) -> str:
    """The stable part of a CfnOutput's key: its construct IDs, alphanumerics only."""
    return re.sub(r"[^A-Za-z0-9]", "", "".join(construct_ids))


def stack_outputs(cfn: Any, stack: str) -> dict[str, str] | None:
    """A stack's outputs keyed by the stable part of each key, or None if it doesn't exist."""
    from botocore.exceptions import ClientError

    try:
        (desc,) = cfn.describe_stacks(StackName=stack)["Stacks"]
    except ClientError as err:
        if "does not exist" in err.response.get("Error", {}).get("Message", ""):
            return None
        raise
    return {_CDK_HASH.sub("", o["OutputKey"]): o["OutputValue"] for o in desc.get("Outputs", [])}


# infra/environment.py: CfnOutput(env construct "Env", "Url")
URL_OUTPUT = cdk_output_id("Env", "Url")


def env_url(cfn: Any, env: str) -> str | None:
    """An env's base URL from its stack's Url output (no trailing slash), or None."""
    from infra.config import env_stack_name

    outputs = stack_outputs(cfn, env_stack_name(env))
    url = outputs.get(URL_OUTPUT) if outputs else None
    return url.rstrip("/") if url else None


def infra_repo_name() -> str:
    """This repo's GitHub name: cdk.json context `infraRepo`, else the default in config."""
    from infra.config import DEFAULT_INFRA_REPO

    context = json.loads((REPO_ROOT / "cdk.json").read_text()).get("context", {})
    return context.get("infraRepo") or DEFAULT_INFRA_REPO


def run(cmd: Sequence[str]) -> int:
    """Run a command in the repo root, output streaming straight to the terminal/CI log."""
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(list(cmd), cwd=REPO_ROOT, check=False).returncode


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1
