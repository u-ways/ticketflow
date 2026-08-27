"""Parse ``depends-on:`` and ``scope:`` blocks from an issue body (ADR-0007).

Pure function. The body is canonical for dependencies; native tracker links
are write-only mirrors. Malformed entries are reported, never guessed at.
"""

import re
from dataclasses import dataclass

# Tracker-native keys: Jira style (PROJ-41), GitHub style (#12), or a bare
# alphanumeric identifier. No whitespace inside a key.
_KEY_RE = re.compile(r"^#?[A-Za-z0-9][A-Za-z0-9_-]*$")
_DEPENDS_RE = re.compile(r"^\s*depends-on:(?P<value>.*)$", re.IGNORECASE)
_SCOPE_RE = re.compile(r"^\s*scope:(?P<value>.*)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """Result of parsing one issue body."""

    depends_on: tuple[str, ...]
    scope: tuple[str, ...]
    issues: tuple[str, ...]


def parse_body(body: str) -> ParsedBody:
    """Extract dependency keys and scope hints from an issue body."""
    depends: list[str] = []
    scope: list[str] = []
    issues: list[str] = []

    for line in body.splitlines():
        if match := _DEPENDS_RE.match(line):
            _parse_keys(match.group("value"), depends, issues)
        elif match := _SCOPE_RE.match(line):
            _parse_paths(match.group("value"), scope, issues)

    return ParsedBody(
        depends_on=tuple(dict.fromkeys(depends)),
        scope=tuple(dict.fromkeys(scope)),
        issues=tuple(issues),
    )


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
