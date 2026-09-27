"""Plan rendering (D7): terraform-plan-style text for CI logs, Markdown for the job summary.

Both show the same facts: the env and what triggered the run, per service the branch
found (or "none found -> main") and its SHA, any fallback and why, and the action.
"""

from reconciler.branches import Ignored, rename_help
from reconciler.core import (
    MAIN_ENV,
    Action,
    Conflict,
    Plan,
    ResolvedService,
    Teardown,
    short,
)

NO_AWS_NOTE = "AWS not checked (--no-aws): SHAs only, no image digests or stack status."


def _title(plan: Plan) -> str:
    return "shared env 'main'" if plan.env == MAIN_ENV else f"preview env '{plan.env}'"


def _found(svc: ResolvedService) -> str:
    return f"{svc.matched.branch} @ {short(svc.matched.sha)}" if svc.matched else "none found"


def _runs(svc: ResolvedService) -> str:
    return f"{svc.ref} @ {short(svc.sha)}"


def _stack_status(plan: Plan) -> str:
    if not plan.stacks_checked:
        return "not checked"
    return plan.stack_status or "does not exist"


def _action_line(plan: Plan) -> str:
    if isinstance(plan.desired, Conflict):
        return "none - refused, env left unchanged (conflict)"
    if not plan.ok:
        return "none - refused, fix the errors above and rerun"
    if plan.decision is None:
        if isinstance(plan.desired, Teardown):
            return "destroy if the stack exists (stack not checked)"
        return "create or update (stack not checked)"
    return f"{plan.decision.action} - {plan.decision.reason}"


def render_text(plan: Plan) -> str:
    lines = [
        f"Plan for {_title(plan)}",
        f"  Trigger: {plan.trigger}",
        f"  Stack:   {plan.stack} ({_stack_status(plan)})",
        "",
    ]

    match plan.desired:
        case Conflict():
            lines += ["ERROR: " + plan.desired.message(), ""]
        case Teardown(env):
            lines += [f"  No service has a branch in group '{env}' -> tear the env down.", ""]

    if plan.resolved is not None:
        width = max(len(s.service) for s in plan.resolved.services) if plan.resolved.services else 0
        for svc in plan.resolved.services:
            if plan.env == MAIN_ENV:
                where = _runs(svc)  # main never looks for branches
            elif svc.matched is None:
                where = f"none found -> {_runs(svc)}"
            elif svc.fell_back:
                where = f"{_found(svc)} -> {_runs(svc)}"
            else:
                where = _found(svc)
            lines.append(f"  {svc.service.ljust(width)}  {where}")
            if svc.note:
                lines.append(f"  {' ' * width}  ! {svc.note}")
            if svc.image:
                lines.append(f"  {' ' * width}    image {svc.image}")
        for error in plan.resolved.errors:
            lines.append(f"  ERROR: {error}")
        for wait in plan.resolved.waiting:
            lines.append(f"  WAITING: {wait}")
        lines.append("")

    if plan.decision is not None and plan.decision.action is Action.BLOCKED:
        lines.append(f"  ERROR: {plan.decision.reason}")
    lines.append(f"  Action: {_action_line(plan)}")
    if not plan.stacks_checked:
        lines.append(f"  Note: {NO_AWS_NOTE}")
    return "\n".join(lines)


def render_markdown(plan: Plan) -> str:
    heading = "Shared env `main`" if plan.env == MAIN_ENV else f"Preview env `{plan.env}`"
    lines = [
        f"### {heading}",
        "",
        f"- **Trigger:** {plan.trigger}",
        f"- **Stack:** `{plan.stack}` ({_stack_status(plan)})",
        "",
    ]

    match plan.desired:
        case Conflict():
            lines += ["> [!CAUTION]", *[f"> {m}" for m in plan.desired.message().splitlines()], ""]
        case Teardown(env):
            lines += [f"No service has a branch in group `{env}` -> tear the env down.", ""]

    if plan.resolved is not None:
        lines += [
            "| Service | Branch found | Runs | Image | Note |",
            "|---|---|---|---|---|",
        ]
        for svc in plan.resolved.services:
            if svc.matched:
                found = f"`{svc.matched.branch}` @ `{short(svc.matched.sha)}`"
            else:
                found = "-" if plan.env == MAIN_ENV else "none found"
            runs = f"`{svc.ref}` @ `{short(svc.sha)}`"
            image = f"`{svc.image.rsplit('@', 1)[-1][:19]}`" if svc.image else "-"
            note = svc.note or ""
            lines.append(f"| {svc.service} | {found} | {runs} | {image} | {note} |")
        lines.append("")
        for error in plan.resolved.errors:
            lines.append(f"**ERROR:** {error}")
        for wait in plan.resolved.waiting:
            lines.append(f"**Waiting:** {wait}")
        if plan.resolved.errors or plan.resolved.waiting:
            lines.append("")

    if plan.decision is not None and plan.decision.action is Action.BLOCKED:
        lines += [f"**ERROR:** {plan.decision.reason}", ""]
    lines.append(f"**Action:** {_action_line(plan)}")
    if not plan.stacks_checked:
        lines += ["", f"_{NO_AWS_NOTE}_"]
    return "\n".join(lines) + "\n"


def render_ignored_text(ignored: Ignored) -> str:
    if ignored.quiet:
        return f"Branch '{ignored.branch}' ignored: {ignored.reason}."
    bar = "!" * 72
    return f"{bar}\n{rename_help(ignored)}\n{bar}"


def render_ignored_markdown(ignored: Ignored) -> str:
    if ignored.quiet:
        return f"Branch `{ignored.branch}` ignored: {ignored.reason}.\n"
    # A fenced block keeps the rename commands copy-pasteable in the job summary.
    warning = f"> [!WARNING]\n> Branch `{ignored.branch}` got no preview env."
    return f"{warning}\n\n```\n{rename_help(ignored)}\n```\n"
