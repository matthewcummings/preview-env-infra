"""Post (or update) the preview URL as a sticky comment on the group's service-repo PRs.

    uv run python scripts/pr_comment.py --group checkout --action update \\
        --url http://... --spec envspec.json --run-url https://... [--smoke passed]
    uv run python scripts/pr_comment.py --group checkout --action destroy --run-url https://...

Run by the infra repo's reconcile workflow after each apply. For every registered service
repo it finds PRs whose head branch is in the group (preview/<group>[/...], same rule as the
reconciler) and keeps ONE comment per PR, found by a hidden marker, up to date:
  - create/update/delete-then-create: open PRs; edit our comment, or add it if missing.
  - destroy: PRs in any state (recent 100), but only ones that already have our comment,
    so a teardown never adds a new comment to an old PR.

Token: PREVIEW_COMMENT_TOKEN, a fine-grained token with Pull requests: Read and write on the
service repos only. Commenting is a courtesy, never a gate: no token -> notice and exit 0;
GitHub errors -> warning and exit 0. Only bad usage exits non-zero (2).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconciler.branches import Group, parse_branch, validate_group  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, Registry, load_registry  # noqa: E402
from reconciler.spec import EnvSpec  # noqa: E402

TOKEN_ENV = "PREVIEW_COMMENT_TOKEN"
API_URL = "https://api.github.com"
DEPLOY_ACTIONS = ("create", "update", "delete-then-create")
ACTIONS = (*DEPLOY_ACTIONS, "destroy")
PAGE = 100

# (method, url, json body or None, token) -> (HTTP status or 0 if unreachable, parsed JSON)
type Http = Callable[[str, str, dict[str, Any] | None, str], tuple[int, Any]]


class GitHubError(Exception):
    pass


def marker(group: str) -> str:
    return f"<!-- preview-env:{group} -->"


def deploy_body(group: str, *, url: str, spec: EnvSpec, run_url: str, smoke: str | None) -> str:
    rows = [f"| {name} | `{svc.ref}` @ `{svc.sha[:7]}` |" for name, svc in spec.services.items()]
    return "\n".join(
        [
            marker(group),
            f"### Preview environment `{group}`",
            "",
            f"**URL:** {url}",
            "",
            "| Service | Runs |",
            "|---|---|",
            *rows,
            "",
            f"**Smoke test:** {smoke or 'not run'} | [Deploy run]({run_url})",
            "",
            "_This comment is updated on every deploy of this environment._",
        ]
    )


def destroy_body(group: str, *, run_url: str) -> str:
    return "\n".join(
        [
            marker(group),
            f"Preview environment `{group}` has been removed. [Teardown run]({run_url})",
        ]
    )


def http_request(method: str, url: str, body: dict[str, Any] | None, token: str) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "preview-env-pr-comment",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        return err.code, None
    except urllib.error.URLError, TimeoutError:
        return 0, None


class Commenter:
    def __init__(self, token: str, http: Http = http_request, log=print) -> None:
        self.token = token
        self.http = http
        self.log = log

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, data = self.http(method, API_URL + path, body, self.token)
        if not 200 <= status < 300:
            raise GitHubError(f"{method} {path} -> {status or 'unreachable'}")
        return data

    def group_prs(self, repo: str, group: str, *, open_only: bool) -> list[dict[str, Any]]:
        """PRs in `repo` whose head branch (in `repo` itself, not a fork) is in the group."""
        state = "open" if open_only else "all&sort=updated&direction=desc"
        pulls = self._call("GET", f"/repos/{repo}/pulls?state={state}&per_page={PAGE}") or []
        return [
            pr
            for pr in pulls
            if parse_branch(pr["head"]["ref"]) == Group(group)
            and (pr["head"].get("repo") or {}).get("full_name") == repo
        ]

    def our_comment(self, repo: str, number: int, group: str) -> dict[str, Any] | None:
        page = 1
        while True:
            comments = (
                self._call(
                    "GET", f"/repos/{repo}/issues/{number}/comments?per_page={PAGE}&page={page}"
                )
                or []
            )
            for comment in comments:
                if marker(group) in (comment.get("body") or ""):
                    return comment
            if len(comments) < PAGE:
                return None
            page += 1

    def upsert(self, repo: str, number: int, group: str, body: str, *, create: bool) -> str:
        existing = self.our_comment(repo, number, group)
        if existing is not None:
            self._call("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", {"body": body})
            return "updated"
        if not create:
            return "skipped (no earlier comment)"
        self._call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
        return "created"


def comment_all(
    commenter: Commenter, registry: Registry, group: str, action: str, body: str
) -> int:
    """Comment on every matching PR. Returns the number of GitHub errors (logged, not raised)."""
    deploy = action in DEPLOY_ACTIONS
    errors = 0
    for service in registry.services:
        repo = f"{registry.github_owner}/{service.repo}"
        try:
            prs = commenter.group_prs(repo, group, open_only=deploy)
            if not prs:
                commenter.log(f"{repo}: no {'open ' if deploy else ''}PRs in group '{group}'")
            for pr in prs:
                result = commenter.upsert(repo, pr["number"], group, body, create=deploy)
                commenter.log(f"{repo}#{pr['number']}: comment {result}")
        except GitHubError as err:
            errors += 1
            commenter.log(f"WARNING: {repo}: {err}; check {TOKEN_ENV}'s access to this repo")
    return errors


def main(
    argv: Sequence[str] | None = None,
    *,
    http: Http = http_request,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--group", required=True)
    parser.add_argument("--action", required=True, choices=ACTIONS)
    parser.add_argument("--url", help="env URL (required unless --action destroy)")
    parser.add_argument("--spec", type=Path, help="deployed EnvSpec (required unless destroy)")
    parser.add_argument("--run-url", required=True, help="link to the workflow run")
    parser.add_argument("--smoke", choices=["passed", "failed", "skipped"])
    parser.add_argument("--registry", type=Path, default=registry_path)
    args = parser.parse_args(argv)

    if args.group == "main":
        print("The main env has no preview PRs; nothing to comment on.")
        return 0
    if (problem := validate_group(args.group)) is not None:
        parser.error(problem)
    if args.action in DEPLOY_ACTIONS:
        if not args.url or not args.spec:
            parser.error(f"--action {args.action} needs --url and --spec")
        try:
            spec = EnvSpec.read(args.spec)
        except (OSError, ValueError, KeyError, TypeError) as err:
            parser.error(f"can't read --spec {args.spec}: {err}")
        body = deploy_body(
            args.group,
            url=args.url,
            spec=spec,
            run_url=args.run_url,
            smoke=args.smoke,
        )
    else:
        body = destroy_body(args.group, run_url=args.run_url)

    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"Notice: {TOKEN_ENV} is not set; skipping PR comments (they're optional).")
        return 0

    registry = load_registry(args.registry)
    errors = comment_all(Commenter(token, http), registry, args.group, args.action, body)
    if errors:
        print(f"WARNING: {errors} repo(s) could not be commented on; the deploy is unaffected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
