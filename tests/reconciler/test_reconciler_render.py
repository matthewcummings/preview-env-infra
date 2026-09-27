"""Rendering snapshots: the plan text in CI logs and the Markdown job summary (D7, D24)."""

from reconciler_fakes import A_FEAT, A_MAIN, B_FEAT, B_MAIN, image

from reconciler.branches import parse_branch
from reconciler.core import Conflict, EnvPlan, Teardown, UseBranch, UseMain, make_plan
from reconciler.render import (
    render_ignored_markdown,
    render_ignored_text,
    render_markdown,
    render_text,
)

MAIN_COMMITS = {"service-a": [A_MAIN], "service-b": [B_MAIN]}


def plan_for(desired, *, status="UPDATE_COMPLETE", aws=True, image_for=image):
    return make_plan(
        trigger="branch preview/checkout/api",
        desired=desired,
        main_commits=MAIN_COMMITS,
        image_for=image_for if aws else None,
        stack_status=status,
        stacks_checked=aws,
    )


A_ONLY = EnvPlan(
    "checkout", {"service-a": UseBranch("preview/checkout/api", A_FEAT), "service-b": UseMain()}
)


def test_text_no_aws_a_only():
    assert render_text(plan_for(A_ONLY, status=None, aws=False)) == "\n".join(
        [
            "Plan for preview env 'checkout'",
            "  Trigger: branch preview/checkout/api",
            "  Stack:   preview-env-checkout (not checked)",
            "",
            "  service-a  preview/checkout/api @ af1af1a",
            "  service-b  none found -> main @ b0b0b0b",
            "",
            "  Action: create or update (stack not checked)",
            "  Note: AWS not checked (--no-aws): SHAs only, no image digests or stack status.",
        ]
    )


def test_text_create_shows_images():
    text = render_text(plan_for(A_ONLY, status=None))
    assert f"    image {image('service-a', A_FEAT)}" in text
    assert "  Stack:   preview-env-checkout (does not exist)" in text
    assert "  Action: create - stack does not exist yet" in text


def test_text_image_fallback_is_flagged():
    both = EnvPlan(
        "checkout",
        {
            "service-a": UseBranch("preview/checkout/api", A_FEAT),
            "service-b": UseBranch("preview/checkout/schema", B_FEAT),
        },
    )
    text = render_text(
        plan_for(both, image_for=lambda s, tag: None if tag == B_FEAT else image(s, tag))
    )
    assert "  service-b  preview/checkout/schema @ bf1bf1b -> main @ b0b0b0b" in text
    assert (
        "! no image yet for bf1bf1b or the branch's last 1 commit(s) -> using main @ b0b0b0b"
        in text
    )
    assert "  Action: update - stack exists (UPDATE_COMPLETE)" in text


def test_text_teardown():
    text = render_text(plan_for(Teardown("checkout")))
    assert "No service has a branch in group 'checkout' -> tear the env down." in text
    assert "Action: destroy - no service has a branch in this group any more" in text


def test_text_rollback_complete():
    text = render_text(plan_for(A_ONLY, status="ROLLBACK_COMPLETE"))
    assert "Action: delete-then-create - stack is ROLLBACK_COMPLETE" in text


def test_text_conflict():
    conflict = Conflict("checkout", {"service-a": ("preview/checkout/api", "preview/checkout/ui")})
    text = render_text(plan_for(conflict))
    assert "ERROR: Group 'checkout' is ambiguous, so env 'checkout' was left unchanged:" in text
    assert "'preview/checkout/api' and 'preview/checkout/ui' both claim" in text
    assert "Action: none - refused, env left unchanged (conflict)" in text


def test_text_main_env():
    main = EnvPlan("main", {"service-a": UseMain(), "service-b": UseMain()})
    text = render_text(plan_for(main))
    assert text.startswith("Plan for shared env 'main'\n")
    assert "  service-a  main @ a0a0a0a" in text
    assert "none found" not in text


def test_markdown_table():
    md = render_markdown(plan_for(A_ONLY))
    assert md.startswith("### Preview env `checkout`\n")
    assert "- **Stack:** `preview-env-checkout` (UPDATE_COMPLETE)" in md
    assert "| Service | Branch found | Runs | Image | Note |" in md
    assert (
        "| service-a | `preview/checkout/api` @ `af1af1a` | `preview/checkout/api` @ `af1af1a` "
        "| `sha256:af1af1addddd` |  |"
    ) in md
    assert "| service-b | none found | `main` @ `b0b0b0b` | `sha256:b0b0b0bddddd` |  |" in md
    assert "**Action:** update - stack exists (UPDATE_COMPLETE)" in md


def test_markdown_conflict_is_a_caution_block():
    conflict = Conflict("checkout", {"service-a": ("preview/checkout/api", "preview/checkout/ui")})
    md = render_markdown(plan_for(conflict))
    assert "> [!CAUTION]\n> Group 'checkout' is ambiguous" in md
    assert "| Service |" not in md


def test_ignored_branch_is_loud():
    text = render_ignored_text(parse_branch("feature/foo"))
    assert "Branch 'feature/foo' is ignored: no preview env." in text
    assert "git branch -m feature/foo preview/foo" in text
    md = render_ignored_markdown(parse_branch("feature/foo"))
    assert md.startswith("> [!WARNING]\n> Branch `feature/foo` got no preview env.")
    assert "```\nBranch 'feature/foo' is ignored" in md


def test_bot_branch_is_one_quiet_line():
    text = render_ignored_text(parse_branch("dependabot/pip/x"))
    assert "\n" not in text
    assert text.startswith("Branch 'dependabot/pip/x' ignored: bot branch")
