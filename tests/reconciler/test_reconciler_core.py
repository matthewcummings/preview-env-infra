"""The prompt's scenarios and the edge cases, against the pure core only."""

import pytest
from reconciler_fakes import (
    A_FEAT,
    A_FEAT_2,
    A_MAIN,
    A_MAIN_OLD,
    B_FEAT,
    B_MAIN,
    REGISTRY,
    image,
)

from reconciler.core import (
    Action,
    Branch,
    Conflict,
    EnvPlan,
    Teardown,
    UseBranch,
    UseMain,
    decide_action,
    desired_env,
    main_env,
    make_plan,
    resolve_images,
)
from reconciler.spec import ServiceSpec

MAIN_COMMITS = {"service-a": [A_MAIN], "service-b": [B_MAIN]}


def all_built(service, sha):
    return image(service, sha)


def branches(a=(), b=()):
    return {"service-a": list(a), "service-b": list(b)}


# --- The four scenarios from the prompt (D7) -----------------------------------------


def test_scenario_a_only():
    desired = desired_env("checkout", REGISTRY, branches(a=[Branch("preview/checkout", A_FEAT)]))
    assert desired == EnvPlan(
        "checkout", {"service-a": UseBranch("preview/checkout", A_FEAT), "service-b": UseMain()}
    )


def test_scenario_b_only():
    desired = desired_env("login", REGISTRY, branches(b=[Branch("preview/login/schema", B_FEAT)]))
    assert desired == EnvPlan(
        "login", {"service-a": UseMain(), "service-b": UseBranch("preview/login/schema", B_FEAT)}
    )


def test_scenario_a_and_b_same_group():
    listing = branches(
        a=[Branch("preview/checkout/api", A_FEAT)], b=[Branch("preview/checkout/schema", B_FEAT)]
    )
    assert desired_env("checkout", REGISTRY, listing) == EnvPlan(
        "checkout",
        {
            "service-a": UseBranch("preview/checkout/api", A_FEAT),
            "service-b": UseBranch("preview/checkout/schema", B_FEAT),
        },
    )


def test_scenario_a_and_b_different_groups():
    # Each repo's listing contains both groups' branches; each env takes only its own.
    listing = branches(
        a=[Branch("preview/cart/api", A_FEAT)], b=[Branch("preview/search/index", B_FEAT)]
    )
    assert desired_env("cart", REGISTRY, listing) == EnvPlan(
        "cart", {"service-a": UseBranch("preview/cart/api", A_FEAT), "service-b": UseMain()}
    )
    assert desired_env("search", REGISTRY, listing) == EnvPlan(
        "search", {"service-a": UseMain(), "service-b": UseBranch("preview/search/index", B_FEAT)}
    )


# --- Lifecycle (D26) ------------------------------------------------------------------


def test_teardown_when_no_branches_remain():
    assert desired_env("checkout", REGISTRY, branches()) == Teardown("checkout")


def test_partial_delete_falls_back_to_main():
    # A's branch merged and was deleted; B's remains -> env stays, A runs main.
    desired = desired_env(
        "checkout", REGISTRY, branches(b=[Branch("preview/checkout/schema", B_FEAT)])
    )
    assert isinstance(desired, EnvPlan)
    assert desired.services["service-a"] == UseMain()
    resolved = resolve_images(desired, main_commits=MAIN_COMMITS, image_for=all_built)
    assert [(s.service, s.ref, s.sha) for s in resolved.services] == [
        ("service-a", "main", A_MAIN),
        ("service-b", "preview/checkout/schema", B_FEAT),
    ]


def test_prefix_neighbours_do_not_join_the_group():
    listing = branches(
        a=[Branch("preview/checkout-wip/x", A_FEAT), Branch("preview/checkoutx", A_FEAT)]
    )
    assert desired_env("checkout", REGISTRY, listing) == Teardown("checkout")


def test_invalid_branches_in_listing_are_not_matched():
    listing = branches(a=[Branch("preview/Checkout/x", A_FEAT)])
    assert desired_env("checkout", REGISTRY, listing) == Teardown("checkout")


def test_missing_service_listing_is_an_error_not_main():
    with pytest.raises(ValueError, match="service-b"):
        desired_env("checkout", REGISTRY, {"service-a": []})


def test_main_env_runs_main_everywhere():
    assert main_env(REGISTRY) == EnvPlan("main", {"service-a": UseMain(), "service-b": UseMain()})


# --- Conflict (D7) ----------------------------------------------------------------------


def test_conflict_two_branches_in_one_repo_claim_the_group():
    listing = branches(
        a=[Branch("preview/checkout/ui", A_FEAT_2), Branch("preview/checkout/api", A_FEAT)],
        b=[Branch("preview/checkout/schema", B_FEAT)],
    )
    desired = desired_env("checkout", REGISTRY, listing)
    assert desired == Conflict(
        "checkout", {"service-a": ("preview/checkout/api", "preview/checkout/ui")}
    )
    message = desired.message()
    assert "'preview/checkout/api' and 'preview/checkout/ui'" in message
    assert "left unchanged" in message


def test_conflict_bare_group_branch_and_described_branch():
    listing = branches(
        a=[Branch("preview/checkout", A_FEAT), Branch("preview/checkout/x", A_FEAT_2)]
    )
    assert isinstance(desired_env("checkout", REGISTRY, listing), Conflict)


def test_conflict_plan_is_not_ok_and_has_no_action():
    listing = branches(a=[Branch("preview/c/1", A_FEAT), Branch("preview/c/2", A_FEAT_2)])
    plan = make_plan(
        trigger="branch preview/c/1",
        desired=desired_env("c", REGISTRY, listing),
        main_commits=MAIN_COMMITS,
        image_for=all_built,
        stack_status="UPDATE_COMPLETE",
        stacks_checked=True,
    )
    assert not plan.ok
    assert plan.decision is None


# --- Images (D24, D26) --------------------------------------------------------------------


def test_branch_with_image_is_pinned_by_digest():
    plan = EnvPlan("c", {"service-a": UseBranch("preview/c", A_FEAT), "service-b": UseMain()})
    resolved = resolve_images(plan, main_commits=MAIN_COMMITS, image_for=all_built)
    spec = resolved.to_env_spec()
    assert spec.services["service-a"] == ServiceSpec(
        "preview/c", A_FEAT, image("service-a", A_FEAT)
    )
    assert spec.services["service-b"] == ServiceSpec("main", B_MAIN, image("service-b", B_MAIN))


def test_image_not_built_yet_falls_back_to_main_and_says_so():
    plan = EnvPlan("c", {"service-a": UseMain(), "service-b": UseBranch("preview/c/s", B_FEAT)})
    built = {("service-a", A_MAIN), ("service-b", B_MAIN)}  # B_FEAT still building

    resolved = resolve_images(
        plan,
        main_commits=MAIN_COMMITS,
        image_for=lambda s, sha: all_built(s, sha) if (s, sha) in built else None,
    )

    b = resolved.services[1]
    assert not resolved.errors
    assert b.fell_back
    assert (b.ref, b.sha, b.image) == ("main", B_MAIN, image("service-b", B_MAIN))
    assert b.matched == UseBranch("preview/c/s", B_FEAT)
    assert "image for bf1bf1b not built yet -> using main @ b0b0b0b" in b.note


def test_main_uses_newest_commit_that_has_an_image():
    # Just merged: main's head image is still building, so use the previous main image.
    plan = EnvPlan("c", {"service-a": UseMain(), "service-b": UseBranch("preview/c", B_FEAT)})
    commits = {"service-a": [A_MAIN, A_MAIN_OLD], "service-b": [B_MAIN]}
    resolved = resolve_images(
        plan,
        main_commits=commits,
        image_for=lambda s, sha: None if sha == A_MAIN else image(s, sha),
    )
    a = resolved.services[0]
    assert (a.ref, a.sha) == ("main", A_MAIN_OLD)
    assert "newest main a0a0a0a not built yet; using a1a1a1a" in a.note


def test_no_image_anywhere_is_an_error():
    plan = EnvPlan("c", {"service-a": UseBranch("preview/c", A_FEAT), "service-b": UseMain()})
    resolved = resolve_images(
        plan,
        main_commits=MAIN_COMMITS,
        image_for=lambda s, sha: image(s, sha) if s == "service-b" else None,
    )
    assert resolved.errors == (
        "service-a: no image for preview/c @ af1af1a yet, "
        "and none of the last 1 main commits has an image",
    )
    with pytest.raises(ValueError):
        resolved.to_env_spec()


def test_no_aws_resolves_shas_only():
    plan = EnvPlan("c", {"service-a": UseBranch("preview/c", A_FEAT), "service-b": UseMain()})
    resolved = resolve_images(plan, main_commits=MAIN_COMMITS, image_for=None)
    assert [(s.ref, s.sha, s.image) for s in resolved.services] == [
        ("preview/c", A_FEAT, None),
        ("main", B_MAIN, None),
    ]
    assert not resolved.images_checked
    with pytest.raises(ValueError, match="--no-aws"):
        resolved.to_env_spec()


# --- Desired vs actual (D14) ---------------------------------------------------------------

ENV = EnvPlan("c", {"service-a": UseMain(), "service-b": UseMain()})


@pytest.mark.parametrize(
    ("desired", "status", "action"),
    [
        (ENV, None, Action.CREATE),
        (ENV, "CREATE_COMPLETE", Action.UPDATE),
        (ENV, "UPDATE_COMPLETE", Action.UPDATE),
        (ENV, "UPDATE_ROLLBACK_COMPLETE", Action.UPDATE),
        (ENV, "ROLLBACK_COMPLETE", Action.DELETE_THEN_CREATE),
        (ENV, "UPDATE_ROLLBACK_FAILED", Action.BLOCKED),
        (ENV, "DELETE_FAILED", Action.BLOCKED),
        (Teardown("c"), None, Action.NOOP),
        (Teardown("c"), "UPDATE_COMPLETE", Action.DESTROY),
        (Teardown("c"), "ROLLBACK_COMPLETE", Action.DESTROY),
        (Teardown("c"), "DELETE_FAILED", Action.DESTROY),  # retry the delete
    ],
)
def test_decide_action(desired, status, action):
    assert decide_action(desired, status).action is action


def test_blocked_stack_makes_plan_not_ok():
    plan = make_plan(
        trigger="t",
        desired=ENV,
        main_commits=MAIN_COMMITS,
        image_for=all_built,
        stack_status="UPDATE_ROLLBACK_FAILED",
        stacks_checked=True,
    )
    assert not plan.ok
    assert "fix or delete the stack by hand" in plan.errors[0]
