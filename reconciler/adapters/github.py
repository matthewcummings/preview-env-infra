"""GitHub REST adapter: which branches exist, and recent main commits (D23, D26).

Stdlib only (urllib) to keep the dependency list short. Unauthenticated calls work for
public repos but are limited to 60 requests/hour, so CI sets GITHUB_TOKEN.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from reconciler.core import Branch
from reconciler.ports import ReconcileError

API_URL = "https://api.github.com"

type Opener = Callable[[urllib.request.Request], Any]


class GitHubBranches:
    def __init__(
        self,
        owner: str,
        *,
        token: str | None = None,
        api_url: str = API_URL,
        opener: Opener | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.owner = owner
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN") or None
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=self.timeout))

    def branches(self, repo: str, prefix: str) -> list[Branch]:
        # matching-refs is a plain prefix match: "preview/checkout" also returns
        # "preview/checkout-wip/...". The core filters by parsed group, so that is fine.
        path = f"/repos/{self.owner}/{repo}/git/matching-refs/heads/{_quote(prefix)}"
        refs = self._get(path, repo)
        return [
            Branch(name=ref["ref"].removeprefix("refs/heads/"), sha=ref["object"]["sha"])
            for ref in refs
        ]

    def main_commits(self, repo: str, limit: int) -> list[str]:
        return self.branch_commits(repo, "main", limit)

    def branch_commits(self, repo: str, branch: str, limit: int) -> list[str]:
        ref = urllib.parse.quote(branch, safe="")
        path = f"/repos/{self.owner}/{repo}/commits?sha={ref}&per_page={limit}"
        return [commit["sha"] for commit in self._get(path, repo)]

    def _get(self, path: str, repo: str) -> Any:
        request = urllib.request.Request(
            self.api_url + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "preview-envs-reconciler",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        full = f"{self.owner}/{repo}"
        try:
            with self._open(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            raise ReconcileError(
                _explain_http_error(err, full, authenticated=bool(self.token))
            ) from err
        except urllib.error.URLError as err:
            raise ReconcileError(f"GitHub API unreachable for {full}: {err.reason}") from err


def _quote(prefix: str) -> str:
    return urllib.parse.quote(prefix, safe="/")


def _explain_http_error(err: urllib.error.HTTPError, repo: str, *, authenticated: bool) -> str:
    base = f"GitHub API {err.code} for {repo} ({err.url})"
    if err.code == 404:
        hint = (
            "the repo does not exist or is not visible to this token. Check github_owner in "
            "services.yaml (or PREVIEW_ENV_GITHUB_OWNER) and the repo names"
        )
        if not authenticated:
            hint += "; private repos need GITHUB_TOKEN"
        return f"{base}: {hint}."
    if err.code == 409:
        return f"{base}: the repo is empty (no commits yet)."
    if err.code in (401, 403, 429):
        if not authenticated:
            return f"{base}: rate limited or forbidden; set GITHUB_TOKEN (unauthenticated: 60/h)."
        return f"{base}: the token was rejected or is rate limited; check GITHUB_TOKEN."
    return f"{base}: {err.reason}."
