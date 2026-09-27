"""Smoke-test a deployed env (D34): is it up, running the planned code, and isolated?

    uv run python scripts/smoke_test.py <env> [--spec envspec.json] [--wait 300]

Per registered service, through the env's ALB (URL from the preview-env-<env> stack output):
  - GET /healthz -> 200, and GET /readyz -> 200 (the service reaches its database)
  - GET /version reports exactly the SHA in --spec (proves the grouping picked the right
    branch per service; skipped without --spec)
  - CRUD round trip: create an item, read it back, delete it, confirm it's gone
  - previews only: the new item is absent from the same service in main (DB isolation)

A new env's targets take a while to pass ALB health checks, and during a rolling update an
old task can still answer for a bit, so the first three checks retry until --wait runs
out. Exit code 1 if any check fails. The caller's IP must be on the ALB allowlist (D42).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import DEFAULT_REGION, MAIN_ENV, env_stack_name  # noqa: E402
from reconciler.registry import DEFAULT_REGISTRY_PATH, load_registry  # noqa: E402
from reconciler.spec import EnvSpec  # noqa: E402
from scripts._common import cdk_output_id, fail, stack_outputs  # noqa: E402

URL_OUTPUT = cdk_output_id("Env", "Url")  # infra/environment.py: CfnOutput(env, "Url")

# (method, url, json body or None) -> (HTTP status or 0 if unreachable, parsed JSON or text)
type Http = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


def http_request(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as err:
        return err.code, _parse(err.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        return 0, str(getattr(err, "reason", err))


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw.decode(errors="replace")[:200]


class Smoke:
    def __init__(
        self,
        http: Http,
        *,
        wait_seconds: float,
        interval: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self.http = http
        self.wait_seconds = wait_seconds
        self.interval = interval
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self.passed = 0
        self.failed = 0
        self._deadline: float | None = None

    def record(self, service: str, ok: bool, what: str) -> bool:
        self.log(f"{'PASS' if ok else 'FAIL'}  {service}  {what}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok

    def eventually(self, service: str, check: Callable[[], tuple[bool, str]]) -> bool:
        """Retry `check` until it passes or the shared warm-up budget runs out."""
        if self._deadline is None:  # one budget for the whole run, started on first use
            self._deadline = self.clock() + self.wait_seconds
        while True:
            ok, what = check()
            if ok or self.clock() >= self._deadline:
                return self.record(service, ok, what)
            self.log(f"...   {service}  {what} (retrying in {self.interval:g}s)")
            self.sleep(self.interval)

    def status_is(self, url: str, path: str, expected: int) -> tuple[bool, str]:
        status, _ = self.http("GET", url + path, None)
        return status == expected, f"GET {path} -> {status or 'unreachable'}"

    def version_is(self, url: str, sha: str) -> tuple[bool, str]:
        status, body = self.http("GET", url + "/version", None)
        got = body.get("sha") if isinstance(body, dict) else None
        return (
            status == 200 and got == sha,
            f"GET /version sha: expected {sha[:7]}, got {got[:7] if got else status}",
        )

    def service(
        self,
        name: str,
        url: str,
        *,
        env: str,
        expected_sha: str | None,
        main_url: str | None,
    ) -> None:
        self.eventually(name, lambda: self.status_is(url, "/healthz", 200))
        self.eventually(name, lambda: self.status_is(url, "/readyz", 200))
        if expected_sha:
            self.eventually(name, lambda: self.version_is(url, expected_sha))
        else:
            self.log(f"SKIP  {name}  /version sha (no --spec)")
        self.crud(name, url, env=env, main_url=main_url)

    def crud(self, name: str, url: str, *, env: str, main_url: str | None) -> None:
        item_name = f"smoke-{env}-{uuid.uuid4().hex[:12]}"
        status, created = self.http(
            "POST", url + "/items", {"name": item_name, "description": "smoke test"}
        )
        item_id = created.get("id") if isinstance(created, dict) else None
        if not self.record(name, status == 201 and item_id is not None, f"POST /items -> {status}"):
            return

        try:
            status, got = self.http("GET", f"{url}/items/{item_id}", None)
            same = isinstance(got, dict) and got.get("name") == item_name
            self.record(name, status == 200 and same, f"GET /items/{item_id} -> {status}")

            if env != MAIN_ENV:
                self.isolation(name, main_url, item_name)
        finally:
            # Always clean up, even if a check above failed.
            status, _ = self.http("DELETE", f"{url}/items/{item_id}", None)
            self.record(name, status == 204, f"DELETE /items/{item_id} -> {status}")
            status, _ = self.http("GET", f"{url}/items/{item_id}", None)
            self.record(name, status == 404, f"GET /items/{item_id} after delete -> {status}")

    def isolation(self, name: str, main_url: str | None, item_name: str) -> None:
        if main_url is None:
            self.record(
                name, False, "isolation: main env URL unknown (is preview-env-main deployed?)"
            )
            return
        status, items = self.http("GET", main_url + "/items", None)
        names = {i.get("name") for i in items} if isinstance(items, list) else None
        ok = status == 200 and names is not None and item_name not in names
        self.record(name, ok, f"isolation: item absent from main's /items ({status})")


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Any = None,
    http: Http = http_request,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("env", help="env name: main or a group")
    parser.add_argument("--spec", type=Path, help="EnvSpec the env was deployed from")
    parser.add_argument("--wait", type=float, default=300, help="warm-up budget, seconds")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    args = parser.parse_args(argv)

    registry = load_registry(args.registry)
    spec = EnvSpec.read(args.spec) if args.spec else None
    if spec is not None and spec.env != args.env:
        return fail(f"--spec is for env '{spec.env}', not '{args.env}'")

    if session is None:
        import boto3

        session = boto3.Session()
    cfn = session.client("cloudformation", region_name=session.region_name or DEFAULT_REGION)

    url = _env_url(cfn, args.env)
    if url is None:
        return fail(f"no URL for env '{args.env}': is {env_stack_name(args.env)} deployed?")
    main_url = url if args.env == MAIN_ENV else _env_url(cfn, MAIN_ENV)
    print(f"Smoke-testing env '{args.env}' at {url}")

    smoke = Smoke(http, wait_seconds=args.wait, sleep=sleep, clock=clock)
    for service in registry.services:
        expected = spec.services[service.name].sha if spec else None
        smoke.service(
            service.name,
            url + service.path_prefix,
            env=args.env,
            expected_sha=expected,
            main_url=main_url + service.path_prefix if main_url else None,
        )

    verdict = "PASSED" if smoke.failed == 0 else "FAILED"
    print(
        f"Smoke test {verdict} for env '{args.env}': {smoke.passed} passed, {smoke.failed} failed"
    )
    return 0 if smoke.failed == 0 else 1


def _env_url(cfn: Any, env: str) -> str | None:
    outputs = stack_outputs(cfn, env_stack_name(env))
    url = outputs.get(URL_OUTPUT) if outputs else None
    return url.rstrip("/") if url else None


if __name__ == "__main__":
    sys.exit(main())
