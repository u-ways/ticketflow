"""Seed (or reset) the ticketflow demo epic on a tracker.

Standalone operator tooling: it populates a tracker with a small, proven
dependency graph — the four-ticket "qacalc" epic — so `ticketflow run` has
something real to execute against a sandbox repository. It talks to the
trackers directly (`gh` CLI for GitHub, plain REST for Jira) and deliberately
imports no vendor SDK: SDK imports live in `src/ticketflow/adapters/` only
(ADR-0002), and this script is not an adapter — it never runs inside the
orchestrator.

Usage (see demo/README.md for the full walkthrough):

    uv run python demo/seed.py seed-github  --repo owner/sandbox \\
        [--project-owner o --project-number 5]
    uv run python demo/seed.py seed-jira    --base-url https://x.atlassian.net --project KAN
    uv run python demo/seed.py reset-github --repo owner/sandbox [--state-dir .ticketflow]
    uv run python demo/seed.py reset-jira   --base-url https://x.atlassian.net --project KAN \\
        [--delete]

Every seeded issue carries the ``tf-demo`` label, which is how reset finds
them. Issues are created in dependency order so ``depends-on:`` lines can
reference the real keys/numbers the tracker just assigned.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEMO_LABEL = "tf-demo"


@dataclass(frozen=True)
class Ticket:
    slug: str
    title: str
    body: str
    depends_on: tuple[str, ...]
    scope: str


EPIC: tuple[Ticket, ...] = (
    Ticket(
        slug="scaffold",
        title="Scaffold the qacalc Python package with tests and CI",
        body=(
            "Create a small Python package called `qacalc` in this repository.\n"
            "\n"
            "Acceptance criteria:\n"
            "- `pyproject.toml` for a package named `qacalc` (any modern build backend; "
            "no runtime dependencies).\n"
            "- `src/qacalc/__init__.py` exposing `add(a, b)` and `subtract(a, b)` with "
            "type hints and docstrings.\n"
            "- `tests/test_operations.py` with pytest tests covering both functions.\n"
            "- A GitHub Actions workflow `.github/workflows/ci.yml` that installs the "
            "package and runs pytest on every push and pull request.\n"
            "- Keep it minimal: no linters, no extra tooling."
        ),
        depends_on=(),
        scope="pyproject.toml, src/, tests/, .github/",
    ),
    Ticket(
        slug="operations",
        title="Add multiply and divide operations",
        body=(
            "Extend qacalc with two more operations.\n"
            "\n"
            "Acceptance criteria:\n"
            "- `multiply(a, b)` and `divide(a, b)` in `src/qacalc/__init__.py`, typed "
            "and documented.\n"
            "- `divide` raises `ZeroDivisionError` with a clear message when b == 0.\n"
            "- pytest tests covering both, including the zero-division case."
        ),
        depends_on=("scaffold",),
        scope="src/qacalc/, tests/",
    ),
    Ticket(
        slug="cli",
        title="Add a command-line interface",
        body=(
            "Give qacalc a CLI.\n"
            "\n"
            "Acceptance criteria:\n"
            "- `python -m qacalc add 2 3` prints `5` (similarly for the other "
            "operations). Use argparse from the standard library; support the "
            "operations that exist on the default branch.\n"
            "- A console-script entry point `qacalc` in pyproject.toml doing the same.\n"
            "- pytest tests covering the CLI (subprocess or direct main() calls)."
        ),
        depends_on=("scaffold",),
        scope="src/qacalc/__main__.py, src/qacalc/cli.py, tests/",
    ),
    Ticket(
        slug="readme",
        title="Write the README with usage examples",
        body=(
            "Document the finished package.\n"
            "\n"
            "Acceptance criteria:\n"
            "- `README.md` at the repo root: what qacalc is, how to install it, a "
            "Python API example for every operation the package exposes, and a CLI "
            "example.\n"
            "- Examples must match the actual code on the default branch (verify them)."
        ),
        depends_on=("operations", "cli"),
        scope="README.md",
    ),
)


def render_body(ticket: Ticket, refs: dict[str, str]) -> str:
    """Ticket body plus its depends-on/scope grammar lines (ADR-0007)."""
    lines = [ticket.body, ""]
    if ticket.depends_on:
        keys = ", ".join(refs[slug] for slug in ticket.depends_on)
        lines.append(f"depends-on: {keys}")
    lines.append(f"scope: {ticket.scope}")
    return "\n".join(lines)


# -- GitHub (via the gh CLI, which owns auth) ------------------------------


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def seed_github(repo: str, project_owner: str | None, project_number: int | None) -> None:
    gh("label", "create", DEMO_LABEL, "-R", repo, "--force", "--color", "5319e7")
    refs: dict[str, str] = {}
    for ticket in EPIC:
        url = gh(
            "issue",
            "create",
            "-R",
            repo,
            "--title",
            ticket.title,
            "--body",
            render_body(ticket, refs),
            "--label",
            DEMO_LABEL,
        )
        number = url.rstrip("/").rsplit("/", 1)[-1]
        refs[ticket.slug] = f"#{number}"
        print(f"created {refs[ticket.slug]}  {ticket.title}")
        if project_owner and project_number:
            try:
                gh(
                    "project",
                    "item-add",
                    str(project_number),
                    "--owner",
                    project_owner,
                    "--url",
                    url,
                )
                print(f"  added to project {project_owner}/{project_number}")
            except subprocess.CalledProcessError as exc:
                print(f"  project add skipped: {exc.stderr.strip()}", file=sys.stderr)
    print(f"\nSeeded {len(EPIC)} issues in {repo}. Point ticketflow at it and run.")


def reset_github(repo: str, state_dir: Path | None) -> None:
    raw = gh(
        "issue",
        "list",
        "-R",
        repo,
        "--label",
        DEMO_LABEL,
        "--state",
        "open",
        "--json",
        "number",
    )
    for item in json.loads(raw or "[]"):
        gh(
            "issue",
            "close",
            str(item["number"]),
            "-R",
            repo,
            "-c",
            "Closed by demo reset.",
        )
        print(f"closed #{item['number']}")

    refs_raw = gh("api", f"repos/{repo}/git/matching-refs/heads/tf/", "--jq", ".[].ref")
    for ref in filter(None, refs_raw.splitlines()):
        gh("api", "-X", "DELETE", f"repos/{repo}/git/{ref}")
        print(f"deleted branch {ref.removeprefix('refs/heads/')}")

    if state_dir is not None and state_dir.is_dir():
        shutil.rmtree(state_dir)
        print(f"removed local state {state_dir}")
    print(
        "\nReset done. The sandbox's default branch keeps any merged demo work — "
        "see demo/README.md for restoring it to a pristine state."
    )


# -- Jira (plain REST; no vendor SDK outside the adapters, ADR-0002) -------


def jira_call(
    base_url: str,
    email: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method=method,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"Jira {method} {path} failed ({exc.code}): {detail}") from exc
    return json.loads(body) if body else {}


def jira_credentials(args: argparse.Namespace) -> tuple[str, str]:
    email = args.email or os.environ.get("ATLASSIAN_EMAIL") or os.environ.get("JIRA_EMAIL")
    token = (
        args.api_token or os.environ.get("ATLASSIAN_API_TOKEN") or os.environ.get("JIRA_API_TOKEN")
    )
    if not email or not token:
        raise SystemExit(
            "Jira credentials missing: pass --email/--api-token or set "
            "ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN."
        )
    return email, token


def seed_jira(base_url: str, project: str, email: str, token: str) -> None:
    refs: dict[str, str] = {}
    for ticket in EPIC:
        created = jira_call(
            base_url,
            email,
            token,
            "POST",
            "/rest/api/2/issue",
            {
                "fields": {
                    "project": {"key": project},
                    "summary": ticket.title,
                    "description": render_body(ticket, refs),
                    "issuetype": {"name": "Task"},
                    "labels": [DEMO_LABEL],
                }
            },
        )
        refs[ticket.slug] = str(created["key"])
        print(f"created {refs[ticket.slug]}  {ticket.title}")
    print(f"\nSeeded {len(EPIC)} issues in {project}. Point ticketflow at it and run.")


def reset_jira(base_url: str, project: str, email: str, token: str, delete: bool) -> None:
    jql = f"project = {project} AND labels = {DEMO_LABEL}"
    if not delete:
        jql += " AND statusCategory != Done"
    query = urllib.parse.urlencode({"jql": jql, "fields": "summary", "maxResults": "50"})
    # /rest/api/2/search was removed by Atlassian (410); search lives at
    # /rest/api/3/search/jql now.
    found = jira_call(base_url, email, token, "GET", f"/rest/api/3/search/jql?{query}")
    for issue in found.get("issues", []):
        key = issue["key"]
        if delete:
            jira_call(base_url, email, token, "DELETE", f"/rest/api/2/issue/{key}")
            print(f"deleted {key}")
            continue
        transitions = jira_call(
            base_url, email, token, "GET", f"/rest/api/2/issue/{key}/transitions"
        ).get("transitions", [])
        done = next(
            (
                t
                for t in transitions
                if t.get("to", {}).get("statusCategory", {}).get("key") == "done"
            ),
            None,
        )
        if done is None:
            print(f"no Done transition for {key}; skipped", file=sys.stderr)
            continue
        jira_call(
            base_url,
            email,
            token,
            "POST",
            f"/rest/api/2/issue/{key}/transitions",
            {"transition": {"id": done["id"]}},
        )
        print(f"moved {key} to Done")
    if not found.get("issues"):
        print("nothing to reset")


# -- entry point -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    seed_gh = commands.add_parser("seed-github", help="Create the demo epic as GitHub issues.")
    seed_gh.add_argument("--repo", required=True, help="owner/name of the sandbox repo")
    seed_gh.add_argument("--project-owner", help="Projects v2 board owner (optional)")
    seed_gh.add_argument("--project-number", type=int, help="Projects v2 board number")

    reset_gh = commands.add_parser("reset-github", help="Close demo issues, delete tf/* branches.")
    reset_gh.add_argument("--repo", required=True)
    reset_gh.add_argument("--state-dir", type=Path, help="local ticketflow state dir to remove")

    seed_jr = commands.add_parser("seed-jira", help="Create the demo epic as Jira issues.")
    reset_jr = commands.add_parser("reset-jira", help="Move demo issues to Done (or delete).")
    for sub in (seed_jr, reset_jr):
        sub.add_argument("--base-url", required=True, help="https://<site>.atlassian.net")
        sub.add_argument("--project", required=True, help="Jira project key")
        sub.add_argument("--email")
        sub.add_argument("--api-token")
    reset_jr.add_argument("--delete", action="store_true", help="hard-delete instead of Done")

    args = parser.parse_args()
    if args.command == "seed-github":
        seed_github(args.repo, args.project_owner, args.project_number)
    elif args.command == "reset-github":
        reset_github(args.repo, args.state_dir)
    elif args.command == "seed-jira":
        email, token = jira_credentials(args)
        seed_jira(args.base_url, args.project, email, token)
    elif args.command == "reset-jira":
        email, token = jira_credentials(args)
        reset_jira(args.base_url, args.project, email, token, args.delete)


if __name__ == "__main__":
    main()
