"""Jira tracker adapter (ADR-0002, spec §7.1).

Translates Jira Cloud issues into canonical :class:`TrackerItem`s, tf-*
signal labels into :class:`TrackerIntent`s, and node states into workflow
transitions. Translate, never decide: no scheduling, retry, or
state-transition logic lives here (ADR-0002); state pushes are a board
projection only. No SDK type crosses the port boundary — every vendor call
sits behind a private seam method, and tests inject a stub client.

Intent consumption has a crash window: a tf-* label is removed from its
issue in the same fetch that emits the intent, so a crash after the label
removal but before the intent row is stored loses that signal. The other
direction is safe — ``external_id`` embeds the issue's ``updated`` stamp,
so re-observing a label that was not yet removed dedupes on ingestion
(ADR-0004).
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any, cast

from ticketflow.config import TrackerConfig
from ticketflow.domain.model import NodeState
from ticketflow.ports.tracker import TrackerCapabilities, TrackerIntent, TrackerItem

_PROVIDER = "jira"
_PAGE_SIZE = 100
_NODE_FIELDS = "summary,description,status,updated"


def _description_text(description: object) -> str:
    """Normalize a Jira description to plain text.

    REST v2 returns a string; Jira Cloud's v3 endpoints return an Atlassian
    Document Format tree (a dict). The depends-on/scope grammar (ADR-0007)
    needs plain lines either way, so ADF is flattened: text nodes joined,
    block-level nodes separated by newlines. Found by the live E2E smoke —
    the adapter must translate at its boundary, whatever the API version.
    """
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        lines: list[str] = []

        def walk(node: dict[str, object]) -> None:
            if node.get("type") == "text":
                text = node.get("text")
                if isinstance(text, str):
                    if lines:
                        lines[-1] += text
                    else:
                        lines.append(text)
            elif node.get("type") == "hardBreak":
                lines.append("")
            content = node.get("content")
            if isinstance(content, list):
                block = node.get("type") in ("paragraph", "heading", "listItem", "codeBlock")
                if block:
                    lines.append("")
                for child in content:
                    if isinstance(child, dict):
                        walk(child)

        walk(description)
        return "\n".join(line for line in lines if line is not None).strip()
    return str(description)


_INTENT_FIELDS = "labels,updated"
_INTENT_LABELS = ("tf-retry", "tf-resume", "tf-unblock", "tf-cancel", "tf-approve", "tf-reject")
_ESCALATED_LABEL = "tf-escalated"
_CURSOR_FORMAT = "%Y-%m-%d %H:%M"

# Board projection targets per node state, in preference order. Matched
# case-insensitively against a transition's target status name or the
# transition's own name; BLOCKED and ESCALATED never transition.
_TRANSITION_TARGETS: dict[NodeState, tuple[str, ...]] = {
    NodeState.READY: ("To Do", "Open", "Backlog", "Selected for Development"),
    NodeState.IN_PROGRESS: ("In Progress",),
    NodeState.AWAITING_SIGNALS: ("In Review", "Review", "In Progress"),
    NodeState.ADDRESSING_FEEDBACK: ("In Progress",),
    NodeState.MERGED: ("Done", "Closed", "Resolved"),
}


class JiraTracker:
    """TrackerPort adapter for Jira Cloud via ``atlassian-python-api``."""

    def __init__(
        self,
        config: TrackerConfig,
        email: str = "",
        api_token: str = "",
        client: Any | None = None,
    ) -> None:
        if not config.project_key:
            raise ValueError("jira tracker requires tracker.project_key")
        self._project_key = config.project_key
        self._issue_type = config.issue_type
        self._client: Any = (
            client if client is not None else _build_client(config, email, api_token)
        )

    def fetch_nodes(self, cursor: str | None) -> tuple[list[TrackerItem], str | None]:
        """Issues updated since the cursor (``YYYY-MM-DD HH:MM``), oldest first."""
        jql = f"project = {self._project_key}"
        if cursor:
            jql += f' AND updated > "{cursor}"'
        jql += " ORDER BY updated ASC"
        items: list[TrackerItem] = []
        latest: datetime | None = None
        for issue in self._search_all(jql, _NODE_FIELDS):
            fields = issue["fields"]
            updated_at = datetime.fromisoformat(fields["updated"])
            items.append(
                TrackerItem(
                    provider=_PROVIDER,
                    external_key=issue["key"],
                    title=fields["summary"],
                    body=_description_text(fields["description"]),
                    etag=fields["updated"],
                    closed=fields["status"]["statusCategory"]["key"] == "done",
                    updated_at=updated_at,
                )
            )
            if latest is None or updated_at > latest:
                latest = updated_at
        next_cursor = latest.strftime(_CURSOR_FORMAT) if latest else cursor
        return items, next_cursor

    def fetch_intents(self, cursor: str | None) -> tuple[list[TrackerIntent], str | None]:
        """Consume tf-* signal labels: emit intents, then strip the labels.

        See the module docstring for the crash window this consumption has.
        The cursor is unused — labels are removed as they are consumed, so
        every fetch starts from a clean board.
        """
        jql = f"project = {self._project_key} AND labels in ({', '.join(_INTENT_LABELS)})"
        intents: list[TrackerIntent] = []
        for issue in self._search_all(jql, _INTENT_FIELDS):
            key: str = issue["key"]
            fields = issue["fields"]
            labels = cast("list[str]", fields.get("labels") or [])
            updated: str = fields["updated"]
            matched = [label for label in _INTENT_LABELS if label in labels]
            for label in matched:
                intents.append(
                    TrackerIntent(
                        external_id=f"jira:{key}:{label}:{updated}",
                        intent_type=label.removeprefix("tf-"),
                        external_key=key,
                    )
                )
            if matched:
                self._set_labels(key, [label for label in labels if label not in matched])
        return intents, cursor

    def push_state(self, external_key: str, state: NodeState) -> None:
        """Project a node state onto the issue's workflow (board projection).

        BLOCKED is a no-op; ESCALATED adds the ``tf-escalated`` label instead
        of transitioning. Other states apply the first workflow transition
        whose target status name or transition name matches a projection
        target; a workflow with no match is a projection gap, not an error.
        """
        if state is NodeState.BLOCKED:
            return
        if state is NodeState.ESCALATED:
            labels = self._issue_labels(external_key)
            if _ESCALATED_LABEL not in labels:
                self._set_labels(external_key, [*labels, _ESCALATED_LABEL])
            return
        transitions = self._transitions(external_key)
        for target in _TRANSITION_TARGETS.get(state, ()):
            for transition in transitions:
                name = str(transition.get("name", "")).casefold()
                to = str(transition.get("to", "")).casefold()
                if target.casefold() in (name, to):
                    self._apply_transition(external_key, transition["id"])
                    return

    def push_comment(self, external_key: str, text: str) -> None:
        """Post a comment on the issue."""
        self._add_comment(external_key, text)

    def create_item(
        self,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        parent_key: str | None = None,
    ) -> str:
        """Create an issue for plan emission (ADR-0014); returns its key.

        ``parent_key`` is ignored: Jira parent/epic linking varies by
        project type and is not mirrored in this slice.
        """
        del parent_key
        fields: dict[str, Any] = {
            "project": {"key": self._project_key},
            "summary": title,
            "description": body,
            "issuetype": {"name": self._issue_type},
        }
        if labels:
            fields["labels"] = list(labels)
        created = self._create_issue(fields)
        return str(created["key"])

    def update_body(self, external_key: str, body: str) -> None:
        """Replace the description (emission phase 2: depends-on lines)."""
        self._update_description(external_key, body)

    def mirror_dependencies(self, external_key: str, depends_on: tuple[str, ...]) -> None:
        """Mirror ``depends-on`` as native ``is blocked by`` links (ADR-0007).

        Write-only and cosmetic; the body stays the only read source. For
        the ``Blocks`` link type the inward issue *is blocked by* the
        outward one, so the child is inward and each upstream is outward.
        Jira treats re-creating an existing link as a no-op, which keeps a
        retried mirror harmless.
        """
        for key in depends_on:
            self._create_issue_link(inward_key=external_key, outward_key=key)

    def capabilities(self) -> TrackerCapabilities:
        """Jira has native links, workflow statuses, and comments (spec §7.1)."""
        return TrackerCapabilities(
            native_dependency_links=True,
            custom_state_field=True,
            supports_comments=True,
        )

    # -- vendor seam: every Jira call lives behind one of these -------------

    def _search_all(self, jql: str, fields: str) -> Iterator[dict[str, Any]]:
        """All issues matching ``jql``, paginating via the response total."""
        start = 0
        while True:
            page = self._search(jql, fields, start)
            issues = cast("list[dict[str, Any]]", page.get("issues") or [])
            yield from issues
            start += len(issues)
            if not issues or start >= int(page.get("total", 0)):
                return

    def _search(self, jql: str, fields: str, start: int) -> dict[str, Any]:
        page = self._client.jql(jql, fields=fields, start=start, limit=_PAGE_SIZE)
        return cast("dict[str, Any]", page or {})

    def _transitions(self, key: str) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._client.get_issue_transitions(key) or [])

    def _apply_transition(self, key: str, transition_id: int | str) -> None:
        self._client.set_issue_status_by_transition_id(key, transition_id)

    def _issue_labels(self, key: str) -> list[str]:
        issue = cast("dict[str, Any]", self._client.get_issue(key, fields="labels") or {})
        return cast("list[str]", (issue.get("fields") or {}).get("labels") or [])

    def _set_labels(self, key: str, labels: list[str]) -> None:
        self._client.update_issue_field(key, {"labels": labels})

    def _add_comment(self, key: str, text: str) -> None:
        self._client.issue_add_comment(key, text)

    def _create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return cast("dict[str, Any]", self._client.issue_create(fields=fields) or {})

    def _update_description(self, key: str, body: str) -> None:
        self._client.update_issue_field(key, {"description": body})

    def _create_issue_link(self, *, inward_key: str, outward_key: str) -> None:
        self._client.create_issue_link(
            {
                "type": {"name": "Blocks"},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            }
        )


def _build_client(config: TrackerConfig, email: str, api_token: str) -> Any:
    """Build the real vendor client; tests always inject a stub instead."""
    if not config.base_url:
        raise ValueError("jira tracker requires tracker.base_url")
    from atlassian import Jira

    return Jira(url=config.base_url, username=email, password=api_token, cloud=True)
