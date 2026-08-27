"""Parse ``depends-on:``, ``scope:`` and ``tf-plan:`` lines from an issue
body (ADR-0007, ADR-0014).

Pure function. The body is canonical for dependencies; native tracker links
are write-only mirrors. Malformed entries are reported, never guessed at.
The ``tf-plan:`` marker tags a ticket emitted by the planner with its
``(plan_id, item_index)`` so partial emissions are identifiable and held.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Tracker-native keys: Jira style (PROJ-41), GitHub style (#12), or a bare
# alphanumeric identifier. No whitespace inside a key.
_KEY_RE = re.compile(r"^#?[A-Za-z0-9][A-Za-z0-9_-]*$")
_DEPENDS_RE = re.compile(r"^\s*depends-on:(?P<value>.*)$", re.IGNORECASE)
_SCOPE_RE = re.compile(r"^\s*scope:(?P<value>.*)$", re.IGNORECASE)
_PLAN_RE = re.compile(r"^\s*tf-plan:(?P<value>.*)$", re.IGNORECASE)
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PLAN_VALUE_RE = re.compile(r"^(?P<plan_id>[0-9a-f]{12})/(?P<index>\d+)$")


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """Result of parsing one issue body."""

    depends_on: tuple[str, ...]
    scope: tuple[str, ...]
    issues: tuple[str, ...]
    plan_marker: tuple[str, int] | None = None
    """``(plan_id, item_index)`` of the emitting plan, when the body carries
    a ``tf-plan:`` marker (ADR-0014)."""


def parse_body(body: str) -> ParsedBody:
    """Extract dependency keys, scope hints and the plan marker from a body."""
    depends: list[str] = []
    scope: list[str] = []
    issues: list[str] = []
    plan_marker: tuple[str, int] | None = None

    for line in body.splitlines():
        if match := _DEPENDS_RE.match(line):
            _parse_keys(match.group("value"), depends, issues)
        elif match := _SCOPE_RE.match(line):
            _parse_paths(match.group("value"), scope, issues)
        elif match := _PLAN_RE.match(line):
            plan_marker = _parse_plan_marker(match.group("value"), plan_marker, issues)

    return ParsedBody(
        depends_on=tuple(dict.fromkeys(depends)),
        scope=tuple(dict.fromkeys(scope)),
        issues=tuple(issues),
        plan_marker=plan_marker,
    )


def render_child_body(
    body: str,
    *,
    plan_id: str,
    item_index: int,
    depends_on: Sequence[str] = (),
    scope: Sequence[str] = (),
) -> str:
    """Render an emitted child ticket's body (ADR-0007, ADR-0014). Pure.

    The output round-trips through :func:`parse_body` byte-for-byte on
    re-render, which is what makes a retried ``update_body`` harmless.
    Anything :func:`parse_body` would report as an issue raises
    :class:`ValueError` here instead — a malformed block is never emitted.
    """
    if not _PLAN_ID_RE.match(plan_id):
        raise ValueError(f"malformed plan id: {plan_id!r}")
    if item_index < 0:
        raise ValueError(f"negative item index: {item_index}")
    for key in depends_on:
        if not _KEY_RE.match(key):
            raise ValueError(f"malformed depends-on key: {key!r}")
    if len(set(depends_on)) != len(depends_on):
        raise ValueError(f"duplicate depends-on keys: {', '.join(depends_on)}")
    for path in scope:
        if not path.strip() or "," in path or "\n" in path:
            raise ValueError(f"malformed scope path: {path!r}")

    lines = [body.rstrip(), "", f"tf-plan: {plan_id}/{item_index}"]
    if depends_on:
        lines.append(f"depends-on: {', '.join(depends_on)}")
    if scope:
        lines.append(f"scope: {', '.join(scope)}")
    return "\n".join(lines)


def _parse_plan_marker(
    value: str, current: tuple[str, int] | None, issues: list[str]
) -> tuple[str, int] | None:
    entry = value.strip()
    match = _PLAN_VALUE_RE.match(entry)
    if match is None:
        issues.append(f"malformed tf-plan marker: {entry!r}")
        return current
    marker = (match.group("plan_id"), int(match.group("index")))
    if current is not None and current != marker:
        issues.append(f"conflicting tf-plan marker: {entry!r}")
        return current
    return marker


def _parse_keys(value: str, out: list[str], issues: list[str]) -> None:
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not entries:
        issues.append("empty depends-on: block (no keys given)")
        return
    for entry in entries:
        if _KEY_RE.match(entry):
            out.append(entry)
        else:
            issues.append(f"malformed depends-on key: {entry!r}")


def _parse_paths(value: str, out: list[str], issues: list[str]) -> None:
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not entries:
        issues.append("empty scope: block (no paths given)")
        return
    out.extend(entries)
