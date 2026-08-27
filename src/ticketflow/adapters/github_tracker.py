"""GitHub Issues tracker adapter (ADR-0002, spec §7.1).

Translates GitHub Issues, repo-wide issue events, labels, and Projects v2 into
the canonical tracker shapes and back. Translate; never decide: no scheduling,
retry, or state-transition logic lives here (ADR-0002). The ``tf:<state>``
label projection is the base and always applies; the Projects v2 Status
projection is best-effort and skipped silently when the board, item, or a
matching option is missing — the labels already carry the state.

Every githubkit call sits behind a small private method so tests can inject a
stub client and never hit the network; no vendor SDK type crosses the port
boundary (ADR-0002 review guidance).
"""

from dataclasses import dataclass
from typing import Any

from githubkit import GitHub

from ticketflow.config import TrackerConfig
from ticketflow.domain.model import IntentType, NodeState
from ticketflow.ports.tracker import TrackerCapabilities, TrackerIntent, TrackerItem

_PROVIDER = "github"
_PER_PAGE = 100

_STATE_LABELS: dict[NodeState, str] = {state: f"tf:{state.value}" for state in NodeState}
_STATE_LABEL_NAMES = frozenset(_STATE_LABELS.values())

_STATE_COLORS: dict[NodeState, str] = {
    NodeState.BLOCKED: "6c757d",
    NodeState.READY: "0e8a16",
    NodeState.IN_PROGRESS: "1d76db",
    NodeState.AWAITING_SIGNALS: "fbca04",
    NodeState.ADDRESSING_FEEDBACK: "d93f0b",
    NodeState.MERGED: "5319e7",
    NodeState.ESCALATED: "b60205",
}

_INTENT_LABELS: dict[str, str] = {f"tf:{intent.value}": intent.value for intent in IntentType}

_STATUS_OPTIONS: dict[NodeState, tuple[str, ...]] = {
    NodeState.BLOCKED: ("Blocked", "Todo", "To Do", "Backlog"),
    NodeState.READY: ("Ready", "Todo", "To Do", "Backlog"),
    NodeState.IN_PROGRESS: ("In Progress", "Doing"),
    NodeState.AWAITING_SIGNALS: ("In Review", "Review", "In Progress"),
    NodeState.ADDRESSING_FEEDBACK: ("In Progress", "Doing"),
    NodeState.MERGED: ("Done", "Merged", "Closed"),
    NodeState.ESCALATED: ("Escalated", "Blocked", "Needs Attention"),
}

_ORG_PROJECT_QUERY = """
query ($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}
"""

_USER_PROJECT_QUERY = """
query ($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}
"""

_PROJECT_ITEM_QUERY = """
query ($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 50) { nodes { id project { id } } }
    }
  }
}
"""

_UPDATE_ITEM_MUTATION = """
mutation ($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $project
      itemId: $item
      fieldId: $field
      value: {singleSelectOptionId: $option}
    }
  ) { projectV2Item { id } }
}
"""


@dataclass(frozen=True, slots=True)
class _ProjectV2:
    """Cached coordinates of the Projects v2 board's Status single-select."""

    project_id: str
    field_id: str
    options: tuple[tuple[str, str], ...]
    """(name, option id) pairs of the Status field's options."""


def _issue_number(external_key: str) -> int:
    """``"#42"`` → ``42`` — the inverse of the external-key projection."""
    return int(external_key.removeprefix("#"))


def _label_names(issue: Any) -> set[str]:
    """Label names on an issue; GitHub returns strings or label objects."""
    names: set[str] = set()
    for label in getattr(issue, "labels", None) or []:
        name = label if isinstance(label, str) else getattr(label, "name", None)
        if name:
            names.add(str(name))
    return names


def _match_option(options: tuple[tuple[str, str], ...], candidates: tuple[str, ...]) -> str | None:
    """First candidate with a case-insensitive Status option name match."""
    by_name: dict[str, str] = {}
    for name, option_id in options:
        by_name.setdefault(name.lower(), option_id)
    for candidate in candidates:
        matched = by_name.get(candidate.lower())
        if matched is not None:
            return matched
    return None


class GitHubTracker:
    """TrackerPort adapter for GitHub Issues + Projects v2 (ADR-0002)."""

    def __init__(
        self, config: TrackerConfig, token: str = "", client: GitHub[Any] | None = None
    ) -> None:
        if not config.repo or "/" not in config.repo:
            raise ValueError("GitHub tracker requires tracker.repo as 'owner/repo'")
        self._owner, self._repo = config.repo.split("/", 1)
        self._config = config
        if client is not None:
            self._client: GitHub[Any] = client
        else:
            self._client = GitHub(token) if token else GitHub()
        self._project_loaded = False
        self._project: _ProjectV2 | None = None

    # -- TrackerPort ---------------------------------------------------------

    def fetch_nodes(self, cursor: str | None) -> tuple[list[TrackerItem], str | None]:
        """Issues updated since the cursor, oldest first; PRs are skipped."""
        items: list[TrackerItem] = []
        page = 1
        while True:
            issues = self._list_issues(page, cursor)
            for issue in issues:
                if getattr(issue, "pull_request", None):
                    continue  # the Issues API interleaves PRs; they are not nodes
                updated_at = issue.updated_at
                items.append(
                    TrackerItem(
                        provider=_PROVIDER,
                        external_key=f"#{issue.number}",
                        title=str(issue.title),
                        body=issue.body or "",
                        etag=updated_at.isoformat(),
                        closed=issue.state == "closed",
                        updated_at=updated_at,
                    )
                )
            if len(issues) < _PER_PAGE:
                break
            page += 1
        latest = max((i.updated_at for i in items if i.updated_at is not None), default=None)
        return items, latest.isoformat() if latest is not None else cursor

    def fetch_intents(self, cursor: str | None) -> tuple[list[TrackerIntent], str | None]:
        """``tf:*`` label events newer than the cursor, as normalized intents."""
        floor = int(cursor) if cursor else 0
        max_id = floor
        seen_any = False
        intents: list[TrackerIntent] = []
        page = 1
        while True:
            events = self._list_issue_events(page)
            page_has_old = False
            for event in events:
                seen_any = True
                event_id = int(event.id)
                max_id = max(max_id, event_id)
                if event_id <= floor:
                    page_has_old = True
                    continue
                if event.event != "labeled":
                    continue
                name = getattr(getattr(event, "label", None), "name", None)
                intent_type = _INTENT_LABELS.get(name or "")
                if intent_type is None:
                    continue
                intents.append(
                    TrackerIntent(
                        external_id=f"github:event:{event_id}",
                        intent_type=intent_type,
                        external_key=f"#{event.issue.number}",
                    )
                )
            # Events arrive newest first: once a page reaches ids at or below
            # the cursor, older pages hold nothing new.
            if len(events) < _PER_PAGE or page_has_old:
                break
            page += 1
        return intents, str(max_id) if seen_any else cursor

    def push_state(self, external_key: str, state: NodeState) -> None:
        """Project a node state: ``tf:*`` label always; Projects v2 if set."""
        number = _issue_number(external_key)
        label = _STATE_LABELS[state]
        self._ensure_label(label, _STATE_COLORS[state])
        issue = self._get_issue(number)
        for name in _label_names(issue):
            if name in _STATE_LABEL_NAMES and name != label:
                self._remove_label(number, name)
        self._add_label(number, label)
        if state is NodeState.MERGED:
            self._close_issue(number)
        elif issue.state == "closed":
            self._reopen_issue(number)
        if self._project_configured():
            self._push_project_state(number, state)

    def push_comment(self, external_key: str, text: str) -> None:
        """Post a comment on the issue behind the external key."""
        self._create_comment(_issue_number(external_key), text)

    def capabilities(self) -> TrackerCapabilities:
        """GitHub Issues: no native links; state field iff a board is set."""
        return TrackerCapabilities(
            native_dependency_links=False,
            custom_state_field=self._project_configured(),
            supports_comments=True,
        )

    # -- Projects v2 Status projection (best-effort) -------------------------

    def _project_configured(self) -> bool:
        return self._config.project_owner is not None and self._config.project_number is not None

    def _push_project_state(self, number: int, state: NodeState) -> None:
        project = self._load_project()
        if project is None:
            return
        option_id = _match_option(project.options, _STATUS_OPTIONS[state])
        if option_id is None:
            return
        item_id = self._find_project_item(number, project.project_id)
        if item_id is None:
            return
        self._graphql(
            _UPDATE_ITEM_MUTATION,
            {
                "project": project.project_id,
                "item": item_id,
                "field": project.field_id,
                "option": option_id,
            },
        )

    def _load_project(self) -> _ProjectV2 | None:
        """Resolve and cache the board's Status field, org then user owner."""
        if self._project_loaded:
            return self._project
        self._project_loaded = True
        variables = {"owner": self._config.project_owner, "number": self._config.project_number}
        for query in (_ORG_PROJECT_QUERY, _USER_PROJECT_QUERY):
            try:
                data = self._graphql(query, variables)
            except Exception:
                continue  # this owner kind does not exist; try the other
            holder = (data.get("organization") or data.get("user")) if data else None
            project = holder.get("projectV2") if holder else None
            status = project.get("field") if project else None
            if not project or not status or "id" not in status:
                continue
            options = tuple((opt["name"], opt["id"]) for opt in status.get("options") or [])
            self._project = _ProjectV2(
                project_id=str(project["id"]), field_id=str(status["id"]), options=options
            )
            return self._project
        return None

    def _find_project_item(self, number: int, project_id: str) -> str | None:
        """The issue's item id on the configured board, or None."""
        try:
            data = self._graphql(
                _PROJECT_ITEM_QUERY,
                {"owner": self._owner, "repo": self._repo, "number": number},
            )
        except Exception:
            return None
        repository = data.get("repository") if data else None
        issue = repository.get("issue") if repository else None
        item_conn = issue.get("projectItems") if issue else None
        nodes = item_conn.get("nodes") if item_conn else None
        for node in nodes or []:
            if (node.get("project") or {}).get("id") == project_id and node.get("id"):
                return str(node["id"])
        return None

    # -- githubkit seam: one thin private method per vendor call -------------

    def _graphql(self, query: str, variables: dict[str, Any]) -> Any:
        graphql: Any = self._client.graphql
        return graphql(query, variables)

    def _list_issues(self, page: int, since: str | None) -> list[Any]:
        rest: Any = self._client.rest
        extra: dict[str, Any] = {"since": since} if since else {}
        resp = rest.issues.list_for_repo(
            self._owner,
            self._repo,
            state="all",
            sort="updated",
            direction="asc",
            per_page=_PER_PAGE,
            page=page,
            **extra,
        )
        return list(resp.parsed_data)

    def _list_issue_events(self, page: int) -> list[Any]:
        rest: Any = self._client.rest
        resp = rest.issues.list_events_for_repo(
            self._owner, self._repo, per_page=_PER_PAGE, page=page
        )
        return list(resp.parsed_data)

    def _get_issue(self, number: int) -> Any:
        rest: Any = self._client.rest
        return rest.issues.get(self._owner, self._repo, number).parsed_data

    def _ensure_label(self, name: str, color: str) -> None:
        rest: Any = self._client.rest
        try:
            rest.issues.create_label(self._owner, self._repo, name=name, color=color)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) != 422:
                raise  # 422 means the label already exists; anything else is real

    def _remove_label(self, number: int, name: str) -> None:
        rest: Any = self._client.rest
        rest.issues.remove_label(self._owner, self._repo, number, name)

    def _add_label(self, number: int, name: str) -> None:
        rest: Any = self._client.rest
        rest.issues.add_labels(self._owner, self._repo, number, labels=[name])

    def _close_issue(self, number: int) -> None:
        rest: Any = self._client.rest
        rest.issues.update(
            self._owner, self._repo, number, state="closed", state_reason="completed"
        )

    def _reopen_issue(self, number: int) -> None:
        rest: Any = self._client.rest
        rest.issues.update(self._owner, self._repo, number, state="open")

    def _create_comment(self, number: int, text: str) -> None:
        rest: Any = self._client.rest
        rest.issues.create_comment(self._owner, self._repo, number, body=text)
