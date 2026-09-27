"""Orchestration and CLI, end to end through the ports with fakes (no network, no AWS)."""

import pytest
from reconciler_fakes import (
    A_FEAT,
    A_FEAT_2,
    A_MAIN,
    B_FEAT,
    B_MAIN,
    REGISTRY,
    FakeDeployer,
    FakeEcr,
    FakeGitHub,
    FakeStacks,
    image,
)

from reconciler.branches import Group, Main
from reconciler.cli import Adapters, main
from reconciler.core import Action, Branch
from reconciler.ports import ReconcileError
from reconciler.reconcile import apply_plan, build_plan
from reconciler.render import render_text
from reconciler.spec import EnvSpec, ServiceSpec


def plan(target, github, ecr=None, stacks=None):
    return build_plan(
        target,
        trigger="test",
        registry=REGISTRY,
        github=github,
        images=ecr,
        stacks=stacks,
    )


def apply(p, github, ecr, deployer):
    return apply_plan(
        p,
        deployer=deployer,
        deleter=deployer,
        log=lambda _: None,
    )


def test_create_same_group_deploys_both_branches():
    github = FakeGitHub(
        {
            "service-a": [Branch("preview/checkout/api", A_FEAT), Branch("main", A_MAIN)],
            "service-b": [Branch("preview/checkout/schema", B_FEAT)],
        }
    )
    p = plan(Group("checkout"), github, FakeEcr(), FakeStacks())
    deployer = FakeDeployer()

    assert apply(p, github, FakeEcr(), deployer) is Action.CREATE
    [(verb, env, spec)] = deployer.calls
    assert (verb, env) == ("deploy", "checkout")
    assert spec.services == {
        "service-a": ServiceSpec("preview/checkout/api", A_FEAT, image("service-a", A_FEAT)),
        "service-b": ServiceSpec("preview/checkout/schema", B_FEAT, image("service-b", B_FEAT)),
    }


def test_rollback_complete_is_deleted_then_created():
    github = FakeGitHub({"service-a": [Branch("preview/checkout", A_FEAT)]})
    p = plan(Group("checkout"), github, FakeEcr(), FakeStacks({"checkout": "ROLLBACK_COMPLETE"}))
    deployer = FakeDeployer()

    assert apply(p, github, FakeEcr(), deployer) is Action.DELETE_THEN_CREATE
    assert [(verb, env) for verb, env, _ in deployer.calls] == [
        ("delete", "checkout"),
        ("deploy", "checkout"),
    ]


def test_teardown_deletes_the_stack_without_a_spec_or_images():
    # D35: teardown must work even when no image exists anywhere.
    github = FakeGitHub({}, main={})
    no_images = FakeEcr(built=set())
    p = plan(Group("checkout"), github, no_images, FakeStacks({"checkout": "UPDATE_COMPLETE"}))
    deployer = FakeDeployer()

    assert apply(p, github, no_images, deployer) is Action.DESTROY
    assert deployer.calls == [("delete", "checkout", None)]
    assert all(call == "branches" for call, _ in github.calls)


def test_teardown_without_stack_is_a_noop():
    deployer = FakeDeployer()
    p = plan(Group("checkout"), FakeGitHub({}), FakeEcr(), FakeStacks())
    assert apply(p, FakeGitHub({}), FakeEcr(), deployer) is Action.NOOP
    assert deployer.calls == []


def test_conflict_refuses_to_apply():
    github = FakeGitHub(
        {"service-a": [Branch("preview/c/1", A_FEAT), Branch("preview/c/2", A_FEAT_2)]}
    )
    p = plan(Group("c"), github, FakeEcr(), FakeStacks({"c": "UPDATE_COMPLETE"}))
    deployer = FakeDeployer()
    with pytest.raises(ReconcileError, match="has errors"):
        apply(p, github, FakeEcr(), deployer)
    assert deployer.calls == []


def test_teardown_does_not_query_main_commits():
    github = FakeGitHub({})
    plan(Group("checkout"), github)
    assert all(call == "branches" for call, _ in github.calls)


def test_main_env_plan():
    p = plan(Main(), FakeGitHub(), FakeEcr(), FakeStacks({"main": "UPDATE_COMPLETE"}))
    assert p.env == "main"
    assert [(s.ref, s.sha) for s in p.resolved.services] == [("main", A_MAIN), ("main", B_MAIN)]
    assert p.decision.action is Action.UPDATE


# --- CLI ------------------------------------------------------------------------------


def fake_factory(github, ecr=None, stacks=None, deployer=None):
    def factory(registry, use_aws):
        if not use_aws:
            return Adapters(github, None, None, None, None)
        return Adapters(github, ecr or FakeEcr(), stacks or FakeStacks(), deployer, deployer)

    return factory


@pytest.fixture
def registry_file(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(
        "github_owner: acme\nservices:\n"
        "  - {name: service-a, repo: service-a, path_prefix: /a, port: 8000, health_path: /h}\n"
        "  - {name: service-b, repo: service-b, path_prefix: /b, port: 8000, health_path: /h}\n"
    )
    return path


def test_cli_plan_no_aws(registry_file, tmp_path, capsys):
    github = FakeGitHub({"service-b": [Branch("preview/login/x", B_FEAT)]})
    summary = tmp_path / "summary.md"
    code = main(
        [
            "plan",
            "--branch",
            "preview/login/x",
            "--no-aws",
            "--registry",
            str(registry_file),
            "--summary-file",
            str(summary),
        ],
        adapters=fake_factory(github),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "service-a  none found -> main @ a0a0a0a" in out
    assert "service-b  preview/login/x @ bf1bf1b" in out
    assert "### Preview env `login`" in summary.read_text()


def test_cli_apply(registry_file, capsys):
    github = FakeGitHub({"service-a": [Branch("preview/checkout", A_FEAT)]})
    deployer = FakeDeployer()
    code = main(
        ["apply", "--group", "checkout", "--registry", str(registry_file)],
        adapters=fake_factory(github, deployer=deployer),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert [c[:2] for c in deployer.calls] == [("deploy", "checkout")]
    assert "Applied: create for env 'checkout'." in out
    assert "EnvUrl: http://checkout.example.com" in out


def test_cli_conflict_exits_nonzero(registry_file, capsys):
    github = FakeGitHub(
        {"service-a": [Branch("preview/c/1", A_FEAT), Branch("preview/c/2", A_FEAT_2)]}
    )
    deployer = FakeDeployer()
    code = main(
        ["apply", "--group", "c", "--registry", str(registry_file)],
        adapters=fake_factory(github, deployer=deployer),
    )
    assert code == 1
    assert deployer.calls == []
    assert "ambiguous" in capsys.readouterr().out


def test_cli_ignored_branch_exits_zero_loudly(registry_file, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    code = main(
        [
            "plan",
            "--branch",
            "feature/foo",
            "--registry",
            str(registry_file),
            "--summary-file",
            str(summary),
        ],
        adapters=fake_factory(FakeGitHub()),
    )
    assert code == 0
    assert "git branch -m feature/foo preview/foo" in capsys.readouterr().out
    assert "[!WARNING]" in summary.read_text()


def test_cli_invalid_group_is_usage_error(registry_file):
    with pytest.raises(SystemExit) as exc:
        main(["plan", "--group", "Bad_Group", "--registry", str(registry_file)])
    assert exc.value.code == 2


def test_cli_apply_rejects_no_aws(registry_file):
    with pytest.raises(SystemExit) as exc:
        main(["apply", "--env", "main", "--no-aws", "--registry", str(registry_file)])
    assert exc.value.code == 2


def test_cli_placeholder_owner_is_a_clear_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PREVIEW_ENV_GITHUB_OWNER", raising=False)
    path = tmp_path / "services.yaml"
    path.write_text("github_owner: CHANGE_ME\nservices: []\n")
    code = main(["plan", "--env", "main", "--no-aws", "--registry", str(path)])
    assert code == 1
    assert "github_owner is still 'CHANGE_ME'" in capsys.readouterr().err


def test_cli_adapter_errors_exit_one(registry_file, capsys):
    class BrokenGitHub(FakeGitHub):
        def branches(self, repo, prefix):
            raise ReconcileError("GitHub API 404 for acme/service-a")

    code = main(
        ["plan", "--group", "x", "--no-aws", "--registry", str(registry_file)],
        adapters=fake_factory(BrokenGitHub()),
    )
    assert code == 1
    assert "ERROR: GitHub API 404 for acme/service-a" in capsys.readouterr().err


# --- --github-output ----------------------------------------------------------------------


def read_outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.mark.parametrize(
    ("stacks", "branches", "expected"),
    [
        ({}, {"service-a": [Branch("preview/checkout", A_FEAT)]}, "create"),
        ({"checkout": "UPDATE_COMPLETE"}, {}, "destroy"),
        ({}, {}, "noop"),
        (
            {"checkout": "UPDATE_COMPLETE"},
            {
                "service-a": [
                    Branch("preview/checkout/1", A_FEAT),
                    Branch("preview/checkout/2", A_FEAT_2),
                ]
            },
            "refused",
        ),
    ],
)
def test_cli_github_output_env_and_action(registry_file, tmp_path, stacks, branches, expected):
    out = tmp_path / "gh_output"
    deployer = FakeDeployer()
    main(
        [
            "apply",
            "--branch",
            "preview/checkout/x",
            "--registry",
            str(registry_file),
            "--github-output",
            str(out),
        ],
        adapters=fake_factory(FakeGitHub(branches), stacks=FakeStacks(stacks), deployer=deployer),
    )
    assert read_outputs(out) == {"env": "checkout", "action": expected}


def test_cli_github_output_no_aws_and_ignored(registry_file, tmp_path):
    out = tmp_path / "gh_output"
    main(
        [
            "plan",
            "--env",
            "main",
            "--no-aws",
            "--registry",
            str(registry_file),
            "--github-output",
            str(out),
        ],
        adapters=fake_factory(FakeGitHub()),
    )
    assert read_outputs(out) == {"env": "main", "action": "unknown"}

    out.unlink()
    main(
        [
            "plan",
            "--branch",
            "feature/x",
            "--registry",
            str(registry_file),
            "--github-output",
            str(out),
        ],
    )
    assert read_outputs(out) == {"env": "", "action": "ignored"}


def test_cli_github_output_on_error(registry_file, tmp_path):
    class BrokenGitHub(FakeGitHub):
        def branches(self, repo, prefix):
            raise ReconcileError("boom")

    out = tmp_path / "gh_output"
    code = main(
        [
            "plan",
            "--group",
            "x",
            "--no-aws",
            "--registry",
            str(registry_file),
            "--github-output",
            str(out),
        ],
        adapters=fake_factory(BrokenGitHub()),
    )
    assert code == 1
    assert read_outputs(out) == {"env": "x", "action": "error"}


# --- --spec-out and teardown ----------------------------------------------------------------


def test_cli_apply_spec_out_keeps_the_deployed_spec(registry_file, tmp_path):
    spec_out = tmp_path / "envspec.json"
    deployer = FakeDeployer()
    code = main(
        [
            "apply",
            "--group",
            "checkout",
            "--registry",
            str(registry_file),
            "--spec-out",
            str(spec_out),
        ],
        adapters=fake_factory(
            FakeGitHub({"service-a": [Branch("preview/checkout", A_FEAT)]}), deployer=deployer
        ),
    )
    assert code == 0
    [(_, _, deployed)] = deployer.calls
    assert EnvSpec.read(spec_out) == deployed
    assert deployed.services["service-a"].sha == A_FEAT


def test_cli_teardown_deletes_even_with_branches_and_warns(registry_file, tmp_path, capsys):
    out = tmp_path / "gh_output"
    deployer = FakeDeployer()
    code = main(
        [
            "teardown",
            "--group",
            "checkout",
            "--registry",
            str(registry_file),
            "--github-output",
            str(out),
        ],
        adapters=fake_factory(
            FakeGitHub({"service-a": [Branch("preview/checkout", A_FEAT)]}), deployer=deployer
        ),
    )
    assert code == 0
    assert deployer.calls == [("delete", "checkout", None)]
    assert "WARNING: group 'checkout' still has preview branches" in capsys.readouterr().out
    assert read_outputs(out) == {"env": "checkout", "action": "destroy"}


def test_cli_teardown_needs_a_group(registry_file):
    with pytest.raises(SystemExit) as exc:
        main(["teardown", "--env", "main", "--registry", str(registry_file)])
    assert exc.value.code == 2


# --- Image fallback through build_plan (D24) ---------------------------------------------------


def test_build_plan_uses_the_newest_built_branch_commit():
    older = "af0af0a" + "e" * 33
    github = FakeGitHub(
        {"service-a": [Branch("preview/checkout/api", A_FEAT)]},
        history={("service-a", "preview/checkout/api"): [A_FEAT, older]},
    )
    ecr = FakeEcr(
        built={
            ("service-a", older),
            ("service-a", f"main-{A_MAIN}"),
            ("service-b", f"main-{B_MAIN}"),
        }
    )
    p = plan(Group("checkout"), github, ecr, FakeStacks({"checkout": "UPDATE_COMPLETE"}))
    a = p.resolved.services[0]
    assert (a.ref, a.sha) == ("preview/checkout/api", older)
    assert ("branch_commits", "service-a") in github.calls
    assert ("branch_commits", "service-b") not in github.calls  # b has no branch
    text = render_text(p)
    assert "preview/checkout/api @ af1af1a -> preview/checkout/api @ af0af0a" in text


def test_no_aws_plan_skips_branch_history():
    github = FakeGitHub({"service-a": [Branch("preview/checkout/api", A_FEAT)]})
    plan(Group("checkout"), github)
    assert not any(call == "branch_commits" for call, _ in github.calls)
