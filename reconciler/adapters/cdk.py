"""CDK runner: how the reconciler creates and updates env stacks (D8, D32).

Deletes do not come through here: they go straight to CloudFormation (D35, adapters/aws.py).

Output is not captured, so CDK's progress streams straight into the CI log.
"""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from reconciler.core import stack_name
from reconciler.ports import ReconcileError

REPO_ROOT = Path(__file__).resolve().parents[2]  # where cdk.json lives

type Runner = Callable[[Sequence[str], Path], int]


def _run(cmd: Sequence[str], cwd: Path) -> int:
    return subprocess.run(list(cmd), cwd=cwd, check=False).returncode


class CdkRunner:
    def __init__(self, *, cwd: Path = REPO_ROOT, runner: Runner = _run) -> None:
        self.cwd = cwd
        self.runner = runner

    def deploy(self, env: str, spec_path: Path) -> None:
        self._cdk(
            "deploy",
            stack_name(env),
            "-c",
            f"envSpec={spec_path}",
            "--require-approval",
            "never",
        )

    def _cdk(self, *args: str) -> None:
        cmd = ["npx", "cdk", *args]
        print(f"$ {' '.join(cmd)}", flush=True)
        code = self.runner(cmd, self.cwd)
        if code != 0:
            raise ReconcileError(f"`{' '.join(cmd)}` failed with exit code {code}")
