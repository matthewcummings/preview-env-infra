"""scripts/smoke_test.py (D34) against an in-memory fake of the two services. No network."""

import pytest
from botocore.stub import Stubber
from script_fakes import FakeSession, client, stack

from reconciler.spec import EnvSpec, ServiceSpec
from scripts import _common, smoke_test
from scripts.smoke_test import Smoke

PREVIEW = "http://preview.example.com"
MAIN = "http://main.example.com"
SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeEnvs:
    """Fake HTTP for the services behind one or more env URLs (each prefix has its own DB)."""

    def __init__(self, shas: dict[str, str], *, warmup_failures: int = 0) -> None:
        self.shas = shas  # base URL (env + path prefix) -> SHA reported by /version
        self.items: dict[str, dict[int, dict]] = {base: {} for base in shas}
        self.warmup_failures = warmup_failures
        self.next_id = 1
        self.requests: list[tuple[str, str]] = []

    def __call__(self, method, url, body):
        self.requests.append((method, url))
        base = next(b for b in self.shas if url.startswith(b + "/"))
        path = url[len(base) :]
        if path == "/healthz":
            if self.warmup_failures:
                self.warmup_failures -= 1
                return 0, "connection refused"
            return 200, {"status": "ok"}
        if path == "/readyz":
            return 200, {"status": "ok"}
        if path == "/version":
            return 200, {"sha": self.shas[base]}
        items = self.items[base]
        if path == "/items":
            if method == "POST":
                item = {"id": self.next_id, **body}
                items[self.next_id] = item
                self.next_id += 1
                return 201, item
            return 200, list(items.values())
        item_id = int(path.rsplit("/", 1)[1])
        if item_id not in items:
            return 404, {"detail": "not found"}
        if method == "DELETE":
            del items[item_id]
            return 204, None
        return 200, items[item_id]


def fake_env_urls():
    return {
        f"{PREVIEW}/a": SHA_A,
        f"{PREVIEW}/b": SHA_B,
        f"{MAIN}/a": "c" * 40,
        f"{MAIN}/b": "d" * 40,
    }


def smoke(http, logs, wait=30):
    clock = iter(range(0, 10_000, 10))
    return Smoke(
        http,
        wait_seconds=wait,
        interval=10,
        sleep=lambda s: None,
        clock=lambda: next(clock),
        log=logs.append,
    )


def test_preview_passes_every_check():
    http, logs = FakeEnvs(fake_env_urls()), []
    s = smoke(http, logs)
    s.service("service-a", f"{PREVIEW}/a", env="checkout", expected_sha=SHA_A, main_url=f"{MAIN}/a")
    assert s.failed == 0, logs
    assert [line.split("  ", 2)[2] for line in logs] == [
        "GET /healthz -> 200",
        "GET /readyz -> 200",
        "GET /version sha: expected aaaaaaa, got aaaaaaa",
        "POST /items -> 201",
        "GET /items/1 -> 200",
        "isolation: item absent from main's /items (200)",
        "DELETE /items/1 -> 204",
        "GET /items/1 after delete -> 404",
    ]
    assert http.items[f"{PREVIEW}/a"] == {}  # cleaned up


def test_waits_for_warmup_within_budget():
    http, logs = FakeEnvs(fake_env_urls(), warmup_failures=2), []
    s = smoke(http, logs)
    s.service("service-a", f"{PREVIEW}/a", env="checkout", expected_sha=SHA_A, main_url=f"{MAIN}/a")
    assert s.failed == 0
    assert sum("retrying" in line for line in logs) == 2


def test_gives_up_after_the_budget():
    http, logs = FakeEnvs(fake_env_urls(), warmup_failures=100), []
    s = smoke(http, logs, wait=30)
    assert not s.eventually("service-a", lambda: s.status_is(f"{PREVIEW}/a", "/healthz", 200))
    assert logs[-1] == "FAIL  service-a  GET /healthz -> unreachable"


def test_wrong_sha_fails():
    http, logs = FakeEnvs(fake_env_urls()), []
    s = smoke(http, logs, wait=0)
    s.service("service-a", f"{PREVIEW}/a", env="checkout", expected_sha=SHA_B, main_url=f"{MAIN}/a")
    assert s.failed == 1
    assert "FAIL  service-a  GET /version sha: expected bbbbbbb, got aaaaaaa" in logs


def test_isolation_failure_is_caught_and_item_still_cleaned_up():
    # Simulate a broken setup where the preview writes into main's database.
    urls = fake_env_urls()
    http = FakeEnvs(urls)
    http.items[f"{PREVIEW}/a"] = http.items[f"{MAIN}/a"]
    logs = []
    s = smoke(http, logs)
    s.service("service-a", f"{PREVIEW}/a", env="checkout", expected_sha=SHA_A, main_url=f"{MAIN}/a")
    assert "FAIL  service-a  isolation: item absent from main's /items (200)" in logs
    assert http.items[f"{MAIN}/a"] == {}  # the finally-block delete still ran


def test_main_env_skips_isolation():
    http, logs = FakeEnvs(fake_env_urls()), []
    s = smoke(http, logs)
    s.service("service-a", f"{MAIN}/a", env="main", expected_sha=None, main_url=f"{MAIN}/a")
    assert s.failed == 0
    assert not any("isolation" in line for line in logs)
    assert "SKIP  service-a  /version sha (no --spec)" in logs


def outputs_stub(stub, env, url):
    stub.add_response(
        "describe_stacks",
        stack(f"preview-env-{env}", {"EnvUrl1E3EC4AF": url}),
        {"StackName": f"preview-env-{env}"},
    )


def test_main_end_to_end(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    EnvSpec(
        "checkout",
        {
            "service-a": ServiceSpec("preview/checkout", SHA_A, "img"),
            "service-b": ServiceSpec("main", SHA_B, "img"),
        },
    ).write(spec)
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        outputs_stub(stub, "checkout", PREVIEW + "/")
        outputs_stub(stub, "main", MAIN)
        code = smoke_test.main(
            ["checkout", "--spec", str(spec)],
            session=FakeSession(cloudformation=cfn),
            http=FakeEnvs(fake_env_urls()),
            sleep=lambda s: None,
        )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Smoke test PASSED for env 'checkout': 16 passed, 0 failed" in out


def test_main_env_not_deployed(capsys):
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_client_error(
            "describe_stacks", "ValidationError", "Stack with id preview-env-x does not exist"
        )
        code = smoke_test.main(["x"], session=FakeSession(cloudformation=cfn), http=FakeEnvs({}))
    assert code == 1
    assert "is preview-env-x deployed?" in capsys.readouterr().err


def test_spec_for_another_env_is_rejected(tmp_path):
    spec = tmp_path / "spec.json"
    EnvSpec("other", {}).write(spec)
    assert smoke_test.main(["checkout", "--spec", str(spec)], session=object()) == 1


@pytest.mark.parametrize("key", ["EnvUrl1E3EC4AF", "EnvUrl"])
def test_url_output_key_ignores_cdk_hash(key):
    cfn = client("cloudformation")
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", stack("preview-env-x", {key: "http://x/"}))
        assert _common.env_url(cfn, "x") == "http://x"
