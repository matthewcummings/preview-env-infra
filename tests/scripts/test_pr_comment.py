"""scripts/pr_comment.py against an in-memory fake of the GitHub REST API. No network."""

import re

import pytest

from reconciler.spec import EnvSpec, ServiceSpec
from scripts import pr_comment

API = "https://api.github.com"
TOKEN = "github_pat_COMMENT_SECRET"
RUN_URL = "https://github.com/octo/preview-env-infra/actions/runs/42"


def pr(number, ref, *, repo="octo/service-a", state="open"):
    return {"number": number, "state": state, "head": {"ref": ref, "repo": {"full_name": repo}}}


class FakeGitHub:
    """Pull requests and issue comments per repo; records every request."""

    def __init__(self, pulls=None, comments=None, fail=None):
        self.pulls = pulls or {}  # repo -> [pr]
        self.comments = comments or {}  # (repo, number) -> [{"id", "body"}]
        self.fail = fail or {}  # repo -> status to return for every call on it
        self.requests = []
        self.next_id = 1000

    def __call__(self, method, url, body, token):
        assert token == TOKEN
        path = url.removeprefix(API)
        self.requests.append((method, path))
        repo = "/".join(path.split("/")[2:4])
        if repo in self.fail:
            return self.fail[repo], None
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls\?state=(\w+).*", path):
            prs = self.pulls.get(m[1], [])
            return 200, [p for p in prs if m[2] == "all" or p["state"] == "open"]
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments(\?.*)?", path):
            key = (m[1], int(m[2]))
            if method == "GET":
                return 200, self.comments.get(key, [])
            self.next_id += 1
            self.comments.setdefault(key, []).append({"id": self.next_id, "body": body["body"]})
            return 201, {"id": self.next_id}
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/comments/(\d+)", path):
            for comments in self.comments.values():
                for c in comments:
                    if c["id"] == int(m[2]):
                        c["body"] = body["body"]
                        return 200, c
            return 404, None
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PATCH")]


@pytest.fixture
def registry_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    path = tmp_path / "services.yaml"
    path.write_text(
        "github_owner: octo\nservices:\n"
        "  - {name: service-a, repo: service-a, path_prefix: /a, port: 8000, health_path: /h}\n"
        "  - {name: service-b, repo: service-b, path_prefix: /b, port: 8000, health_path: /h}\n"
    )
    return path


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "envspec.json"
    EnvSpec(
        "checkout",
        {
            "service-a": ServiceSpec("preview/checkout/api", "a" * 40, "img"),
            "service-b": ServiceSpec("main", "b" * 40, "img"),
        },
    ).write(path)
    return path


def deploy(registry_file, spec_file, http, action="update", smoke="passed"):
    return pr_comment.main(
        [
            "--group", "checkout", "--action", action, "--url", "http://env.example.com",
            "--spec", str(spec_file), "--run-url", RUN_URL, "--smoke", smoke,
            "--registry", str(registry_file),
        ],
        http=http,
    )  # fmt: skip


def destroy(registry_file, http):
    return pr_comment.main(
        ["--group", "checkout", "--action", "destroy", "--run-url", RUN_URL,
         "--registry", str(registry_file)],
        http=http,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("PREVIEW_COMMENT_TOKEN", TOKEN)


def test_deploy_creates_comment_on_matching_open_prs_only(registry_file, spec_file, capsys):
    gh = FakeGitHub(
        pulls={
            "octo/service-a": [
                pr(1, "preview/checkout/api"),
                pr(2, "preview/checkout-wip/x"),  # prefix neighbour: another group
                pr(3, "feature/checkout"),
                pr(4, "preview/checkout/fork", repo="someone/service-a"),  # fork: not ours
            ],
            "octo/service-b": [pr(7, "preview/checkout", repo="octo/service-b")],
        }
    )
    assert deploy(registry_file, spec_file, gh) == 0
    assert gh.writes() == [
        ("POST", "/repos/octo/service-a/issues/1/comments"),
        ("POST", "/repos/octo/service-b/issues/7/comments"),
    ]
    body = gh.comments[("octo/service-a", 1)][0]["body"]
    assert body.startswith("<!-- preview-env:checkout -->\n### Preview environment `checkout`")
    assert "**URL:** http://env.example.com" in body
    assert "| service-a | `preview/checkout/api` @ `aaaaaaa` |" in body
    assert "| service-b | `main` @ `bbbbbbb` |" in body
    assert f"**Smoke test:** passed | [Deploy run]({RUN_URL})" in body
    assert "updated on every deploy" in body
    assert "octo/service-a#1: comment created" in capsys.readouterr().out


def test_deploy_edits_the_existing_sticky_comment(registry_file, spec_file):
    gh = FakeGitHub(
        pulls={"octo/service-a": [pr(1, "preview/checkout/api")]},
        comments={
            ("octo/service-a", 1): [
                {"id": 5, "body": "LGTM"},
                {"id": 6, "body": "<!-- preview-env:checkout-2 -->\nanother group's comment"},
                {"id": 7, "body": "<!-- preview-env:checkout -->\nold"},
            ]
        },
    )
    assert deploy(registry_file, spec_file, gh, smoke="failed") == 0
    assert gh.writes() == [("PATCH", "/repos/octo/service-a/issues/comments/7")]
    comments = gh.comments[("octo/service-a", 1)]
    assert "**Smoke test:** failed" in comments[2]["body"]
    assert comments[1]["body"].endswith("another group's comment")  # marker is exact


def test_destroy_only_updates_prs_that_already_have_our_comment(registry_file, capsys):
    gh = FakeGitHub(
        pulls={
            "octo/service-a": [
                pr(1, "preview/checkout/api", state="closed"),
                pr(2, "preview/checkout/old", state="closed"),
            ]
        },
        comments={("octo/service-a", 1): [{"id": 9, "body": "<!-- preview-env:checkout -->"}]},
    )
    assert destroy(registry_file, gh) == 0
    assert (
        "GET",
        "/repos/octo/service-a/pulls?state=all&sort=updated&direction=desc&per_page=100",
    ) in gh.requests
    assert gh.writes() == [("PATCH", "/repos/octo/service-a/issues/comments/9")]
    assert "has been removed" in gh.comments[("octo/service-a", 1)][0]["body"]
    assert "octo/service-a#2: comment skipped (no earlier comment)" in capsys.readouterr().out


def test_no_token_is_a_notice_and_success(registry_file, spec_file, monkeypatch, capsys):
    monkeypatch.delenv("PREVIEW_COMMENT_TOKEN")
    gh = FakeGitHub()
    assert deploy(registry_file, spec_file, gh) == 0
    assert gh.requests == []
    assert "PREVIEW_COMMENT_TOKEN is not set; skipping PR comments" in capsys.readouterr().out


def test_http_errors_warn_but_never_fail(registry_file, spec_file, capsys):
    gh = FakeGitHub(
        pulls={"octo/service-b": [pr(7, "preview/checkout", repo="octo/service-b")]},
        fail={"octo/service-a": 403},
    )
    assert deploy(registry_file, spec_file, gh) == 0
    out = capsys.readouterr().out
    assert "WARNING: octo/service-a: GET /repos/octo/service-a/pulls" in out
    assert "-> 403" in out
    assert "octo/service-b#7: comment created" in out  # the other repo still got its comment
    assert "WARNING: 1 repo(s) could not be commented on; the deploy is unaffected." in out
    assert TOKEN not in out


def test_deploy_without_url_or_spec_is_usage_error(registry_file):
    with pytest.raises(SystemExit) as exc:
        pr_comment.main(
            ["--group", "checkout", "--action", "create", "--run-url", RUN_URL,
             "--registry", str(registry_file)],
            http=FakeGitHub(),
        )  # fmt: skip
    assert exc.value.code == 2


def test_invalid_group_is_usage_error(registry_file):
    with pytest.raises(SystemExit) as exc:
        pr_comment.main(
            ["--group", "Bad_Group", "--action", "destroy", "--run-url", RUN_URL,
             "--registry", str(registry_file)],
            http=FakeGitHub(),
        )  # fmt: skip
    assert exc.value.code == 2


def test_main_env_has_nothing_to_comment_on(registry_file, capsys):
    gh = FakeGitHub()
    code = pr_comment.main(
        ["--group", "main", "--action", "update", "--run-url", RUN_URL,
         "--registry", str(registry_file)],
        http=gh,
    )  # fmt: skip
    assert code == 0
    assert gh.requests == []


def test_comment_pagination_finds_marker_on_a_later_page(registry_file, spec_file):
    many = [{"id": i, "body": "chatter"} for i in range(100)]
    gh = FakeGitHub(pulls={"octo/service-a": [pr(1, "preview/checkout")]})

    pages = {1: many, 2: [{"id": 500, "body": "<!-- preview-env:checkout -->"}]}
    original = gh.__call__

    def http(method, url, body, token):
        if method == "GET" and "/issues/1/comments" in url:
            gh.requests.append((method, url.removeprefix(API)))
            return 200, pages[int(url.rsplit("page=", 1)[1])]
        if "issues/comments/500" in url:
            gh.requests.append((method, url.removeprefix(API)))
            return 200, {}
        return original(method, url, body, token)

    assert deploy(registry_file, spec_file, http) == 0
    assert gh.writes() == [("PATCH", "/repos/octo/service-a/issues/comments/500")]
