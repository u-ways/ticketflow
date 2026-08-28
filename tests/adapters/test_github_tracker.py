"""GitHubTracker translates GitHub Issues into the canonical shapes (ADR-0002).

The adapter is exercised through an injected stub client carrying exactly the
attribute paths the adapter calls (``rest.issues.*`` and ``graphql``) — no
network, no vendor SDK mocking beyond the seam the adapter itself defines.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from githubkit import GitHub

from ticketflow.adapters.github_tracker import GitHubTracker
from ticketflow.config import TrackerConfig
from ticketflow.domain.model import NodeState
from ticketflow.ports.tracker import TrackerPort


class StubResponse:
    def __init__(self, parsed_data: Any) -> None:
        self.parsed_data = parsed_data


class StubHttpError(Exception):
    """Shaped like githubkit's RequestFailed: carries response.status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


def _issue(
    number: int,
    *,
    title: str = "title",
    body: str | None = "body",
    state: str = "open",
    updated: str = "2026-08-27T10:00:00+00:00",
    labels: list[Any] | None = None,
    pull_request: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        id=number * 1000,  # database id, distinct from the issue number
        node_id=f"NODE{number}",
        title=title,
        body=body,
        state=state,
        updated_at=datetime.fromisoformat(updated),
        labels=labels or [],
        pull_request=pull_request,
    )


def _event(
    event_id: int,
    *,
    event: str = "labeled",
    label: str | None = "tf:retry",
    issue_number: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id,
        event=event,
        label=SimpleNamespace(name=label) if label is not None else None,
        issue=SimpleNamespace(number=issue_number),
    )


class StubIssuesApi:
    def __init__(self) -> None:
        self.issue_pages: list[list[Any]] = []
        self.event_pages: list[list[Any]] = []
        self.issue: Any = _issue(1)
        self.create_label_error: Exception | None = None
        self.list_calls: list[dict[str, Any]] = []
        self.event_calls: list[dict[str, Any]] = []
        self.created_labels: list[tuple[str, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.added_labels: list[tuple[int, tuple[str, ...]]] = []
        self.updates: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.creates: list[dict[str, Any]] = []
        self.issues_by_number: dict[int, Any] = {}
        self.next_created_number = 101

    def list_for_repo(self, owner: str, repo: str, **kwargs: Any) -> StubResponse:
        self.list_calls.append({"owner": owner, "repo": repo, **kwargs})
        page = int(kwargs["page"])
        return StubResponse(self.issue_pages[page - 1] if page <= len(self.issue_pages) else [])

    def list_events_for_repo(self, owner: str, repo: str, **kwargs: Any) -> StubResponse:
        self.event_calls.append({"owner": owner, "repo": repo, **kwargs})
        page = int(kwargs["page"])
        return StubResponse(self.event_pages[page - 1] if page <= len(self.event_pages) else [])

    def get(self, _owner: str, _repo: str, issue_number: int) -> StubResponse:
        return StubResponse(self.issues_by_number.get(issue_number, self.issue))

    def create(self, owner: str, repo: str, **kwargs: Any) -> StubResponse:
        self.creates.append({"owner": owner, "repo": repo, **kwargs})
        created = _issue(self.next_created_number, title=kwargs.get("title", ""))
        self.next_created_number += 1
        return StubResponse(created)

    def create_label(self, _owner: str, _repo: str, *, name: str, color: str) -> None:
        self.created_labels.append((name, color))
        if self.create_label_error is not None:
            raise self.create_label_error

    def remove_label(self, _owner: str, _repo: str, issue_number: int, name: str) -> None:
        self.removed_labels.append((issue_number, name))

    def add_labels(self, _owner: str, _repo: str, issue_number: int, *, labels: list[str]) -> None:
        self.added_labels.append((issue_number, tuple(labels)))

    def update(self, _owner: str, _repo: str, issue_number: int, **kwargs: Any) -> None:
        self.updates.append({"issue_number": issue_number, **kwargs})

    def create_comment(self, _owner: str, _repo: str, issue_number: int, *, body: str) -> None:
        self.comments.append((issue_number, body))


class StubGraphQL:
    """Routes by query shape; a response set to an Exception is raised."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.org_response: Any = None
        self.user_response: Any = None
        self.items_response: Any = None
        self.blocked_org_response: Any = None
        self.blocked_user_response: Any = None
        self.mutations: list[dict[str, Any]] = []
        self.added_items: list[dict[str, Any]] = []
        self.add_item_response: Any = None

    def __call__(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append((query, dict(variables or {})))
        if "updateProjectV2ItemFieldValue" in query:
            self.mutations.append(dict(variables or {}))
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item"}}}
        if "addProjectV2ItemById" in query:
            self.added_items.append(dict(variables or {}))
            if isinstance(self.add_item_response, Exception):
                raise self.add_item_response
            return {"addProjectV2ItemById": {"item": {"id": "ADDED"}}}
        if "projectItems" in query:
            return self._resolve(self.items_response)
        if 'field(name: "Blocked by")' in query:
            if "organization(" in query:
                return self._resolve(self.blocked_org_response)
            return self._resolve(self.blocked_user_response)
        if "organization(" in query:
            return self._resolve(self.org_response)
        return self._resolve(self.user_response)

    @staticmethod
    def _resolve(value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def project_queries(self) -> list[str]:
        return [q for q, _ in self.calls if "projectV2(number:" in q]


class StubClient:
    def __init__(self) -> None:
        self.issues = StubIssuesApi()
        self.rest = SimpleNamespace(issues=self.issues)
        self.graphql = StubGraphQL()
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.request_error: Exception | None = None

    def request(self, method: str, url: str, json: dict[str, Any] | None = None) -> None:
        self.requests.append((method, url, json))
        if self.request_error is not None:
            raise self.request_error


def org_project_payload(options: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "organization": {
            "projectV2": {
                "id": "P1",
                "field": {
                    "id": "F1",
                    "options": [{"id": oid, "name": name} for name, oid in options],
                },
            }
        }
    }


def items_payload(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}


def make_tracker(*, project: bool = False) -> tuple[GitHubTracker, StubClient]:
    config = TrackerConfig(
        provider="github",
        repo="acme/widgets",
        project_owner="acme" if project else None,
        project_number=7 if project else None,
    )
    stub = StubClient()
    return GitHubTracker(config, client=cast("GitHub[Any]", stub)), stub


class TestConstruction:
    def test_repo_is_required(self) -> None:
        with pytest.raises(ValueError, match="owner/repo"):
            GitHubTracker(TrackerConfig(provider="github"))

    def test_repo_must_be_owner_slash_repo(self) -> None:
        with pytest.raises(ValueError, match="owner/repo"):
            GitHubTracker(TrackerConfig(provider="github", repo="widgets"))

    def test_builds_real_client_when_none_injected(self) -> None:
        config = TrackerConfig(provider="github", repo="acme/widgets")
        assert isinstance(GitHubTracker(config, token="tok")._client, GitHub)
        assert isinstance(GitHubTracker(config)._client, GitHub)

    def test_satisfies_the_tracker_port(self) -> None:
        tracker, _ = make_tracker()
        port: TrackerPort = tracker  # static protocol conformance (ADR-0002)
        assert port.capabilities().supports_comments


class TestFetchNodes:
    def test_maps_issues_to_canonical_items(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue_pages = [
            [
                _issue(12, title="Build the parser", body="details"),
                _issue(13, body=None, state="closed", updated="2026-08-27T11:30:00+00:00"),
            ]
        ]
        items, cursor = tracker.fetch_nodes(None)
        assert [i.external_key for i in items] == ["#12", "#13"]
        first, second = items
        assert first.provider == "github"
        assert first.title == "Build the parser"
        assert first.body == "details"
        assert not first.closed
        assert first.updated_at == datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
        assert first.etag == "2026-08-27T10:00:00+00:00"
        assert second.body == ""  # None body normalized at the edge
        assert second.closed
        assert cursor == "2026-08-27T11:30:00+00:00"  # max updated_at wins

    def test_skips_pull_requests(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue_pages = [
            [_issue(1), _issue(2, pull_request=SimpleNamespace(url="https://x"))]
        ]
        items, _ = tracker.fetch_nodes(None)
        assert [i.external_key for i in items] == ["#1"]

    def test_passes_cursor_as_since_and_omits_when_none(self) -> None:
        tracker, stub = make_tracker()
        tracker.fetch_nodes("2026-08-27T09:00:00+00:00")
        tracker.fetch_nodes(None)
        with_cursor, without_cursor = stub.issues.list_calls
        assert with_cursor["since"] == "2026-08-27T09:00:00+00:00"
        assert with_cursor["state"] == "all"
        assert with_cursor["sort"] == "updated"
        assert with_cursor["direction"] == "asc"
        assert with_cursor["per_page"] == 100
        assert "since" not in without_cursor

    def test_no_items_keeps_the_old_cursor(self) -> None:
        tracker, _ = make_tracker()
        items, cursor = tracker.fetch_nodes("2026-08-27T09:00:00+00:00")
        assert items == []
        assert cursor == "2026-08-27T09:00:00+00:00"

    def test_paginates_past_full_pages(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue_pages = [
            [_issue(n) for n in range(1, 101)],
            [_issue(101, updated="2026-08-27T12:00:00+00:00")],
        ]
        items, cursor = tracker.fetch_nodes(None)
        assert len(items) == 101
        assert [c["page"] for c in stub.issues.list_calls] == [1, 2]
        assert cursor == "2026-08-27T12:00:00+00:00"


class TestFetchIntents:
    @pytest.mark.parametrize(
        ("label", "intent_type"),
        [
            ("tf:retry", "retry"),
            ("tf:resume", "resume"),
            ("tf:unblock", "unblock"),
            ("tf:cancel", "cancel"),
            ("tf:approve", "approve"),
            ("tf:reject", "reject"),
        ],
    )
    def test_maps_tf_labels_to_intents(self, label: str, intent_type: str) -> None:
        tracker, stub = make_tracker()
        stub.issues.event_pages = [[_event(42, label=label, issue_number=9)]]
        intents, cursor = tracker.fetch_intents(None)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.external_id == "github:event:42"
        assert intent.intent_type == intent_type
        assert intent.external_key == "#9"
        assert cursor == "42"

    def test_ignores_non_intent_events(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.event_pages = [
            [
                _event(10, event="closed", label=None),
                _event(11, label="bug"),
                _event(12, label="tf:nonsense"),
                _event(13, label=None),
                _event(14, label="tf:approve"),
            ]
        ]
        intents, cursor = tracker.fetch_intents(None)
        assert [i.external_id for i in intents] == ["github:event:14"]
        assert cursor == "14"  # ignored events still advance the cursor

    def test_cursor_filters_already_seen_events(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.event_pages = [
            [_event(150), _event(100, label="tf:cancel"), _event(90, label="tf:cancel")]
        ]
        intents, cursor = tracker.fetch_intents("100")
        assert [i.external_id for i in intents] == ["github:event:150"]
        assert cursor == "150"

    def test_no_events_keeps_the_old_cursor(self) -> None:
        tracker, _ = make_tracker()
        assert tracker.fetch_intents(None) == ([], None)
        assert tracker.fetch_intents("77") == ([], "77")

    def test_paginates_past_full_pages(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.event_pages = [
            [_event(i) for i in range(1000, 900, -1)],
            [_event(900, label="tf:resume")],
        ]
        intents, cursor = tracker.fetch_intents(None)
        assert len(intents) == 101
        assert [c["page"] for c in stub.issues.event_calls] == [1, 2]
        assert cursor == "1000"

    def test_stops_paginating_once_a_page_reaches_the_cursor(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.event_pages = [
            [_event(i) for i in range(1000, 900, -1)],
            [_event(i) for i in range(900, 800, -1)],
        ]
        intents, cursor = tracker.fetch_intents("950")
        assert len(intents) == 50  # 1000..951
        assert [c["page"] for c in stub.issues.event_calls] == [1]
        assert cursor == "1000"


class TestPushState:
    def test_ensures_label_and_adds_it(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue = _issue(5)
        tracker.push_state("#5", NodeState.READY)
        assert stub.issues.created_labels == [("tf:ready", "0e8a16")]
        assert stub.issues.added_labels == [(5, ("tf:ready",))]
        assert stub.issues.removed_labels == []
        assert stub.issues.updates == []  # open issue, non-terminal state

    def test_removes_stale_state_labels_but_not_intent_labels(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue = _issue(
            5, labels=[SimpleNamespace(name="tf:in_progress"), "tf:retry", "bug", "tf:ready"]
        )
        tracker.push_state("#5", NodeState.READY)
        assert stub.issues.removed_labels == [(5, "tf:in_progress")]
        assert stub.issues.added_labels == [(5, ("tf:ready",))]

    def test_label_already_exists_conflict_is_ignored(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.create_label_error = StubHttpError(422)
        tracker.push_state("#5", NodeState.BLOCKED)
        assert stub.issues.added_labels == [(5, ("tf:blocked",))]

    def test_other_label_creation_errors_propagate(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.create_label_error = StubHttpError(500)
        with pytest.raises(StubHttpError):
            tracker.push_state("#5", NodeState.BLOCKED)

    def test_merged_closes_the_issue_as_completed(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue = _issue(8)
        tracker.push_state("#8", NodeState.MERGED)
        assert stub.issues.updates == [
            {"issue_number": 8, "state": "closed", "state_reason": "completed"}
        ]

    def test_non_merged_state_reopens_a_closed_issue(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issue = _issue(8, state="closed")
        tracker.push_state("#8", NodeState.READY)
        assert stub.issues.updates == [{"issue_number": 8, "state": "open"}]

    def test_no_project_configured_makes_no_graphql_calls(self) -> None:
        tracker, stub = make_tracker()
        tracker.push_state("#5", NodeState.READY)
        assert stub.graphql.calls == []


class TestProjectProjection:
    OPTIONS: ClassVar[list[tuple[str, str]]] = [
        ("Todo", "O1"),
        ("In Progress", "O2"),
        ("Done", "O3"),
    ]

    def _project_tracker(self) -> tuple[GitHubTracker, StubClient]:
        tracker, stub = make_tracker(project=True)
        stub.graphql.org_response = org_project_payload(self.OPTIONS)
        stub.graphql.items_response = items_payload(
            [{"id": "OTHER", "project": {"id": "P-other"}}, {"id": "I1", "project": {"id": "P1"}}]
        )
        return tracker, stub

    def test_happy_path_updates_the_status_field(self) -> None:
        tracker, stub = self._project_tracker()
        tracker.push_state("#5", NodeState.IN_PROGRESS)
        assert stub.graphql.mutations == [
            {"project": "P1", "item": "I1", "field": "F1", "option": "O2"}
        ]

    def test_option_match_is_case_insensitive_and_ordered(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.graphql.org_response = org_project_payload([("done", "O3"), ("in progress", "O2")])
        stub.graphql.items_response = items_payload([{"id": "I1", "project": {"id": "P1"}}])
        # AWAITING_SIGNALS candidates: In Review, Review, In Progress — the
        # first present option wins, matched case-insensitively.
        tracker.push_state("#5", NodeState.AWAITING_SIGNALS)
        assert [m["option"] for m in stub.graphql.mutations] == ["O2"]

    def test_missing_option_skips_silently(self) -> None:
        tracker, stub = self._project_tracker()
        tracker.push_state("#5", NodeState.ESCALATED)  # no Escalated/Blocked/Needs Attention
        assert stub.graphql.mutations == []
        assert stub.issues.added_labels == [(5, ("tf:escalated",))]  # labels still applied

    def test_missing_project_item_is_added_then_updated(self) -> None:
        # A fresh board starts empty: the projection adds the issue to the
        # project (what a human board does) and then sets its Status.
        tracker, stub = self._project_tracker()
        stub.issues.issues_by_number[5] = _issue(5)
        stub.graphql.items_response = items_payload([{"id": "X", "project": {"id": "P-other"}}])
        tracker.push_state("#5", NodeState.READY)
        assert stub.graphql.added_items == [{"project": "P1", "content": "NODE5"}]
        assert stub.graphql.mutations == [
            {"project": "P1", "item": "ADDED", "field": "F1", "option": "O1"}
        ]

    def test_add_item_failure_skips_silently(self) -> None:
        tracker, stub = self._project_tracker()
        stub.graphql.items_response = items_payload([])
        stub.graphql.add_item_response = StubHttpError(403)
        tracker.push_state("#5", NodeState.READY)
        assert stub.graphql.mutations == []
        assert stub.issues.added_labels == [(5, ("tf:ready",))]

    def test_item_lookup_failure_falls_through_to_the_idempotent_add(self) -> None:
        # addProjectV2ItemById returns the existing item for known content,
        # so adding on a failed lookup is harmless and self-healing.
        tracker, stub = self._project_tracker()
        stub.graphql.items_response = StubHttpError(500)
        tracker.push_state("#5", NodeState.READY)
        assert len(stub.graphql.added_items) == 1
        assert [m["item"] for m in stub.graphql.mutations] == ["ADDED"]

    def test_missing_project_skips_silently(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.graphql.org_response = StubHttpError(404)
        stub.graphql.user_response = {"user": None}
        tracker.push_state("#5", NodeState.READY)
        assert stub.graphql.mutations == []
        assert stub.issues.added_labels == [(5, ("tf:ready",))]

    def test_user_owned_project_is_found_after_org_lookup_fails(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.graphql.org_response = StubHttpError(404)
        payload = org_project_payload(self.OPTIONS)
        stub.graphql.user_response = {"user": payload["organization"]}
        stub.graphql.items_response = items_payload([{"id": "I1", "project": {"id": "P1"}}])
        tracker.push_state("#5", NodeState.MERGED)
        assert [m["option"] for m in stub.graphql.mutations] == ["O3"]

    def test_project_lookup_is_cached_across_pushes(self) -> None:
        tracker, stub = self._project_tracker()
        tracker.push_state("#5", NodeState.READY)
        tracker.push_state("#5", NodeState.IN_PROGRESS)
        assert len(stub.graphql.project_queries) == 1
        assert len(stub.graphql.mutations) == 2

    def test_failed_project_lookup_is_cached_too(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.graphql.org_response = StubHttpError(404)
        stub.graphql.user_response = StubHttpError(404)
        tracker.push_state("#5", NodeState.READY)
        tracker.push_state("#5", NodeState.READY)
        assert len(stub.graphql.project_queries) == 2  # org + user, once only


class TestPushComment:
    def test_posts_comment_on_the_issue(self) -> None:
        tracker, stub = make_tracker()
        tracker.push_comment("#41", "escalated: needs a human")
        assert stub.issues.comments == [(41, "escalated: needs a human")]


class TestCapabilities:
    def test_without_project_board(self) -> None:
        tracker, _ = make_tracker()
        caps = tracker.capabilities()
        # Native blocked-by relationships went GA in 2025 (ADR-0002 revision).
        assert caps.native_dependency_links
        assert not caps.custom_state_field
        assert caps.supports_comments

    def test_with_project_board(self) -> None:
        tracker, _ = make_tracker(project=True)
        assert tracker.capabilities().custom_state_field


def blocked_field_payload(owner_kind: str = "organization") -> dict[str, Any]:
    return {owner_kind: {"projectV2": {"id": "P1", "field": {"id": "FB"}}}}


class TestCreateItem:
    def test_creates_issue_with_ensured_labels(self) -> None:
        tracker, stub = make_tracker()
        key = tracker.create_item("Build it", "the body", labels=("tf-plan-abc",))
        assert key == "#101"
        assert stub.issues.created_labels == [("tf-plan-abc", "ededed")]
        create = stub.issues.creates[0]
        assert create["title"] == "Build it"
        assert create["body"] == "the body"
        assert create["labels"] == ["tf-plan-abc"]

    def test_parent_key_attaches_sub_issue(self) -> None:
        tracker, stub = make_tracker()
        tracker.create_item("Child", "b", parent_key="#42")
        method, url, payload = stub.requests[0]
        assert method == "POST"
        assert url == "/repos/acme/widgets/issues/42/sub_issues"
        assert payload == {"sub_issue_id": 101000}  # the created issue's db id

    def test_sub_issue_failure_is_swallowed(self) -> None:
        # The hierarchy mirror is cosmetic: the created key still returns.
        tracker, stub = make_tracker()
        stub.request_error = StubHttpError(404)
        assert tracker.create_item("Child", "b", parent_key="#42") == "#101"


class TestUpdateBody:
    def test_updates_issue_body(self) -> None:
        tracker, stub = make_tracker()
        tracker.update_body("#7", "new body\n\ndepends-on: #5")
        assert stub.issues.updates == [{"issue_number": 7, "body": "new body\n\ndepends-on: #5"}]


class TestMirrorDependencies:
    def test_native_blocked_by_per_upstream(self) -> None:
        tracker, stub = make_tracker()
        stub.issues.issues_by_number = {7: _issue(7), 9: _issue(9)}
        tracker.mirror_dependencies("#12", ("#7", "#9"))
        assert stub.requests == [
            ("POST", "/repos/acme/widgets/issues/12/dependencies/blocked_by", {"issue_id": 7000}),
            ("POST", "/repos/acme/widgets/issues/12/dependencies/blocked_by", {"issue_id": 9000}),
        ]

    def test_no_upstreams_is_a_noop(self) -> None:
        tracker, stub = make_tracker()
        tracker.mirror_dependencies("#12", ())
        assert stub.requests == []

    def test_falls_back_to_blocked_by_field_on_board(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.request_error = StubHttpError(404)  # native relationships unavailable
        stub.graphql.blocked_org_response = blocked_field_payload()
        stub.graphql.items_response = items_payload([{"id": "I1", "project": {"id": "P1"}}])
        tracker.mirror_dependencies("#12", ("#7",))
        assert stub.graphql.mutations == [
            {"project": "P1", "item": "I1", "field": "FB", "text": "#7"}
        ]

    def test_raises_when_native_fails_and_no_board(self) -> None:
        tracker, stub = make_tracker()
        stub.request_error = StubHttpError(404)
        with pytest.raises(RuntimeError, match="mirror"):
            tracker.mirror_dependencies("#12", ("#7",))

    def test_raises_when_fallback_field_missing(self) -> None:
        tracker, stub = make_tracker(project=True)
        stub.request_error = StubHttpError(404)
        stub.graphql.blocked_org_response = StubHttpError(404)
        stub.graphql.blocked_user_response = StubHttpError(404)
        with pytest.raises(RuntimeError, match="mirror"):
            tracker.mirror_dependencies("#12", ("#7",))
