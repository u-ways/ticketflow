"""Jira tracker adapter tests (ADR-0002).

The adapter is exercised through a stub client injected at the seam — no
vendor SDK is imported and no network is touched. The stub records every
call so the tests can assert exact translation, never behaviour.
"""

from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest

from ticketflow.adapters.jira_tracker import JiraTracker
from ticketflow.config import TrackerConfig
from ticketflow.domain.model import NodeState

UPDATED = "2026-08-27T14:05:32.000+0000"


class StubJiraClient:
    """Canned-response stand-in for ``atlassian.Jira``; records every call."""

    def __init__(self) -> None:
        self.jql_calls: list[dict[str, Any]] = []
        self.jql_pages: deque[dict[str, Any]] = deque()
        self.transition_reads: list[str] = []
        self.transitions: dict[str, list[dict[str, Any]]] = {}
        self.transitions_applied: list[tuple[str, int | str]] = []
        self.issue_reads: list[tuple[str, str | None]] = []
        self.issues: dict[str, dict[str, Any]] = {}
        self.field_updates: list[tuple[str, dict[str, Any]]] = []
        self.comments: list[tuple[str, str]] = []

    def jql(
        self, jql: str, fields: str = "*all", start: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        self.jql_calls.append({"jql": jql, "fields": fields, "start": start, "limit": limit})
        if self.jql_pages:
            return self.jql_pages.popleft()
        return {"issues": [], "total": 0, "startAt": start}

    def get_issue_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        self.transition_reads.append(issue_key)
        return self.transitions.get(issue_key, [])

    def set_issue_status_by_transition_id(self, issue_key: str, transition_id: int | str) -> None:
        self.transitions_applied.append((issue_key, transition_id))

    def get_issue(self, issue_id_or_key: str, fields: str | None = None) -> dict[str, Any]:
        self.issue_reads.append((issue_id_or_key, fields))
        return self.issues[issue_id_or_key]

    def update_issue_field(self, key: str, fields: dict[str, Any]) -> None:
        self.field_updates.append((key, fields))

    def issue_add_comment(self, issue_key: str, comment: str) -> None:
        self.comments.append((issue_key, comment))


def make_config(**overrides: Any) -> TrackerConfig:
    base: dict[str, Any] = {
        "provider": "jira",
        "base_url": "https://example.atlassian.net",
        "project_key": "PROJ",
    }
    base.update(overrides)
    return TrackerConfig.model_validate(base)


def make_tracker(client: StubJiraClient) -> JiraTracker:
    return JiraTracker(make_config(), client=client)


def issue(
    key: str,
    *,
    summary: str = "A task",
    description: str | None = "Task body",
    category: str = "new",
    updated: str = UPDATED,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "status": {"statusCategory": {"key": category}},
        "updated": updated,
    }
    if labels is not None:
        fields["labels"] = labels
    return {"key": key, "fields": fields}


def page(*issues: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    return {"issues": list(issues), "total": total if total is not None else len(issues)}


class TestConstructor:
    def test_requires_project_key(self) -> None:
        with pytest.raises(ValueError, match="project_key"):
            JiraTracker(make_config(project_key=None), client=StubJiraClient())

    def test_default_client_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            JiraTracker(make_config(base_url=None))


class TestFetchNodes:
    def test_jql_without_cursor(self) -> None:
        client = StubJiraClient()
        make_tracker(client).fetch_nodes(None)
        assert client.jql_calls == [
            {
                "jql": "project = PROJ ORDER BY updated ASC",
                "fields": "summary,description,status,updated",
                "start": 0,
                "limit": 100,
            }
        ]

    def test_jql_with_cursor(self) -> None:
        client = StubJiraClient()
        make_tracker(client).fetch_nodes("2026-08-27 14:05")
        assert client.jql_calls[0]["jql"] == (
            'project = PROJ AND updated > "2026-08-27 14:05" ORDER BY updated ASC'
        )

    def test_maps_issue_to_canonical_item(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(page(issue("PROJ-41")))
        items, _ = make_tracker(client).fetch_nodes(None)
        [item] = items
        assert item.provider == "jira"
        assert item.external_key == "PROJ-41"
        assert item.title == "A task"
        assert item.body == "Task body"
        assert item.etag == UPDATED
        assert item.closed is False
        assert item.updated_at == datetime(2026, 8, 27, 14, 5, 32, tzinfo=UTC)

    def test_closed_via_done_status_category(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(
            page(issue("PROJ-1", category="done"), issue("PROJ-2", category="indeterminate"))
        )
        items, _ = make_tracker(client).fetch_nodes(None)
        assert [item.closed for item in items] == [True, False]

    def test_none_description_becomes_empty_body(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(page(issue("PROJ-1", description=None)))
        items, _ = make_tracker(client).fetch_nodes(None)
        assert items[0].body == ""

    def test_paginates_via_total(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(page(issue("PROJ-1"), issue("PROJ-2"), total=3))
        client.jql_pages.append(page(issue("PROJ-3"), total=3))
        items, _ = make_tracker(client).fetch_nodes(None)
        assert [item.external_key for item in items] == ["PROJ-1", "PROJ-2", "PROJ-3"]
        assert [call["start"] for call in client.jql_calls] == [0, 2]

    def test_next_cursor_is_max_updated(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(
            page(
                issue("PROJ-1", updated="2026-08-27T15:10:45.000+0000"),
                issue("PROJ-2", updated="2026-08-27T09:00:01.000+0000"),
            )
        )
        _, cursor = make_tracker(client).fetch_nodes(None)
        assert cursor == "2026-08-27 15:10"

    def test_empty_result_keeps_cursor(self) -> None:
        client = StubJiraClient()
        items, cursor = make_tracker(client).fetch_nodes("2026-08-27 14:05")
        assert items == []
        assert cursor == "2026-08-27 14:05"


class TestFetchIntents:
    def test_jql_targets_the_intent_labels(self) -> None:
        client = StubJiraClient()
        make_tracker(client).fetch_intents(None)
        assert client.jql_calls[0]["jql"] == (
            "project = PROJ AND labels in "
            "(tf-retry, tf-resume, tf-unblock, tf-cancel, tf-approve, tf-reject)"
        )

    def test_emits_one_intent_per_tf_label_and_strips_them(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(page(issue("PROJ-7", labels=["keep", "tf-retry", "tf-approve"])))
        intents, _ = make_tracker(client).fetch_intents(None)
        assert [(i.intent_type, i.external_key) for i in intents] == [
            ("retry", "PROJ-7"),
            ("approve", "PROJ-7"),
        ]
        assert [i.external_id for i in intents] == [
            f"jira:PROJ-7:tf-retry:{UPDATED}",
            f"jira:PROJ-7:tf-approve:{UPDATED}",
        ]
        # One update per issue, dropping only the consumed tf-* labels.
        assert client.field_updates == [("PROJ-7", {"labels": ["keep"]})]

    def test_external_ids_are_stable_across_refetch(self) -> None:
        def fetch_once() -> list[str]:
            client = StubJiraClient()
            client.jql_pages.append(page(issue("PROJ-7", labels=["tf-unblock"])))
            intents, _ = make_tracker(client).fetch_intents(None)
            return [i.external_id for i in intents]

        assert fetch_once() == fetch_once()

    def test_issue_without_tf_labels_is_ignored(self) -> None:
        client = StubJiraClient()
        client.jql_pages.append(page(issue("PROJ-8", labels=["keep"])))
        intents, _ = make_tracker(client).fetch_intents(None)
        assert intents == []
        assert client.field_updates == []

    def test_cursor_is_passed_through_unchanged(self) -> None:
        client = StubJiraClient()
        _, cursor = make_tracker(client).fetch_intents("opaque-cursor")
        assert cursor == "opaque-cursor"


class TestPushState:
    def test_matches_on_target_status_name(self) -> None:
        client = StubJiraClient()
        client.transitions["PROJ-1"] = [
            {"name": "Start", "id": 11, "to": "In Progress"},
            {"name": "Reopen", "id": 12, "to": "To Do"},
        ]
        make_tracker(client).push_state("PROJ-1", NodeState.READY)
        assert client.transitions_applied == [("PROJ-1", 12)]

    def test_matches_on_transition_name(self) -> None:
        client = StubJiraClient()
        client.transitions["PROJ-1"] = [{"name": "Done", "id": 31, "to": "Finished"}]
        make_tracker(client).push_state("PROJ-1", NodeState.MERGED)
        assert client.transitions_applied == [("PROJ-1", 31)]

    def test_matching_is_case_insensitive(self) -> None:
        client = StubJiraClient()
        client.transitions["PROJ-1"] = [{"name": "start", "id": 21, "to": "in progress"}]
        make_tracker(client).push_state("PROJ-1", NodeState.IN_PROGRESS)
        assert client.transitions_applied == [("PROJ-1", 21)]

    def test_prefers_earlier_target_name_over_transition_order(self) -> None:
        client = StubJiraClient()
        client.transitions["PROJ-1"] = [
            {"name": "Back to work", "id": 41, "to": "In Progress"},
            {"name": "Review", "id": 42, "to": "In Review"},
        ]
        make_tracker(client).push_state("PROJ-1", NodeState.AWAITING_SIGNALS)
        assert client.transitions_applied == [("PROJ-1", 42)]

    def test_no_matching_transition_is_a_noop(self) -> None:
        client = StubJiraClient()
        client.transitions["PROJ-1"] = [{"name": "Weird", "id": 51, "to": "Limbo"}]
        make_tracker(client).push_state("PROJ-1", NodeState.MERGED)
        assert client.transitions_applied == []

    def test_blocked_is_a_noop(self) -> None:
        client = StubJiraClient()
        make_tracker(client).push_state("PROJ-1", NodeState.BLOCKED)
        assert client.transition_reads == []
        assert client.transitions_applied == []
        assert client.field_updates == []

    def test_escalated_adds_label_instead_of_transitioning(self) -> None:
        client = StubJiraClient()
        client.issues["PROJ-9"] = {"fields": {"labels": ["keep"]}}
        make_tracker(client).push_state("PROJ-9", NodeState.ESCALATED)
        assert client.transition_reads == []
        assert client.issue_reads == [("PROJ-9", "labels")]
        assert client.field_updates == [("PROJ-9", {"labels": ["keep", "tf-escalated"]})]

    def test_escalated_label_is_not_duplicated(self) -> None:
        client = StubJiraClient()
        client.issues["PROJ-9"] = {"fields": {"labels": ["tf-escalated"]}}
        make_tracker(client).push_state("PROJ-9", NodeState.ESCALATED)
        assert client.field_updates == []


class TestPushComment:
    def test_posts_comment_on_issue(self) -> None:
        client = StubJiraClient()
        make_tracker(client).push_comment("PROJ-3", "hello from ticketflow")
        assert client.comments == [("PROJ-3", "hello from ticketflow")]


class TestCapabilities:
    def test_declares_jira_native_features(self) -> None:
        caps = make_tracker(StubJiraClient()).capabilities()
        assert caps.native_dependency_links is True
        assert caps.custom_state_field is True
        assert caps.supports_comments is True


class TestAdfDescription:
    def test_v3_adf_description_is_flattened_to_lines(self) -> None:
        # Jira Cloud v3 returns ADF; the depends-on grammar needs plain lines.
        from ticketflow.adapters.jira_tracker import _description_text

        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Do the thing."}]},
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "depends-on: KAN-9, KAN-10"}],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "scope: src/"}]},
            ],
        }
        text = _description_text(adf)
        from ticketflow.domain.parser import parse_body

        parsed = parse_body(text)
        assert parsed.depends_on == ("KAN-9", "KAN-10")
        assert parsed.scope == ("src/",)

    def test_plain_string_and_none_pass_through(self) -> None:
        from ticketflow.adapters.jira_tracker import _description_text

        assert _description_text("plain") == "plain"
        assert _description_text(None) == ""

    def test_hard_breaks_split_lines(self) -> None:
        # A single paragraph with hardBreak nodes (how Jira stores literal
        # newlines) must still yield one grammar line per break.
        from ticketflow.adapters.jira_tracker import _description_text
        from ticketflow.domain.parser import parse_body

        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Intro."},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "depends-on: KAN-9"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "scope: src/"},
                    ],
                }
            ],
        }
        parsed = parse_body(_description_text(adf))
        assert parsed.depends_on == ("KAN-9",)
        assert parsed.scope == ("src/",)
