import pytest

from reconciler.branches import Group, Ignored, Main, parse_branch, rename_help, suggest_group


@pytest.mark.parametrize(
    ("branch", "group"),
    [
        ("preview/checkout", "checkout"),
        ("preview/checkout/cart-api", "checkout"),
        ("preview/checkout/SHOP-123/anything/else", "checkout"),
        ("preview/login-fix", "login-fix"),
        ("preview/a", "a"),
        ("preview/9", "9"),
        ("preview/" + "x" * 20, "x" * 20),
        ("refs/heads/preview/checkout/api", "checkout"),
    ],
)
def test_valid_preview_branches_join_their_group(branch, group):
    assert parse_branch(branch) == Group(group)


def test_main_is_the_shared_env():
    assert parse_branch("main") == Main()
    assert parse_branch("refs/heads/main") == Main()


@pytest.mark.parametrize(
    "branch",
    [
        "preview/Checkout/api",  # uppercase: no normalization, so Foo and foo never collide
        "preview/check_out",
        "preview/-checkout",
        "preview/checkout-",
        "preview/" + "x" * 21,  # 21 chars
        "preview/check.out",
        "preview/",
        "preview//api",
        "preview",
    ],
)
def test_invalid_group_names_are_ignored_loudly(branch):
    result = parse_branch(branch)
    assert isinstance(result, Ignored)
    assert not result.quiet


def test_main_group_is_reserved():
    result = parse_branch("preview/main/whatever")
    assert isinstance(result, Ignored)
    assert not result.quiet
    assert "reserved" in result.reason


@pytest.mark.parametrize("branch", ["feature/whatever", "fix-tests", "mainline", "Main"])
def test_non_preview_branches_are_ignored_loudly(branch):
    result = parse_branch(branch)
    assert isinstance(result, Ignored)
    assert not result.quiet
    assert "preview/" in result.reason


@pytest.mark.parametrize("branch", ["dependabot/pip/boto3-1.2.3", "renovate/pytest-9.x"])
def test_bot_branches_are_ignored_quietly(branch):
    result = parse_branch(branch)
    assert isinstance(result, Ignored)
    assert result.quiet


def test_rename_help_explains_rule_and_shows_commands():
    ignored = parse_branch("feature/Cart_API")
    assert isinstance(ignored, Ignored)
    text = rename_help(ignored)
    assert "Branch 'feature/Cart_API' is ignored: no preview env." in text
    assert "1-20 characters of a-z, 0-9 and '-'" in text
    assert "git branch -m feature/Cart_API preview/cart-api" in text
    assert "git push origin --delete feature/Cart_API" in text


def test_suggest_group_only_offers_valid_names():
    assert suggest_group("feature/Cart_API") == "cart-api"
    assert suggest_group("preview/Checkout/x") == "x"
    assert suggest_group("feature/main") is None  # reserved
    assert suggest_group("feature/___") is None
