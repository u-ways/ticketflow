"""GitHubCodeHost adapter tests (ADR-0002): a stub client, never the network.

The stub stands in for the githubkit ``GitHub`` client behind the adapter's
injectable seam — it scripts ``rest.*`` responses and the ``graphql`` callable
and records every call, so tests assert pure translation.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from githubkit import GitHub
from githubkit.exception import GraphQLError, RequestFailed

from ticketflow.adapters.github_codehost import GitHubCodeHost
from ticketflow.ports.codehost import CheckState, CodeHostPort, ReviewDecision


def request_failed(status: int) -> RequestFailed:
    request = httpx.Request("GET", "https://api.github.invalid/stub")
    raw = httpx.Response(status_code=status, request=request)
    stub = SimpleNamespace(raw_request=request, raw_response=raw, status_code=status)
    return RequestFailed(cast(Any, stub))


class Resp:
    """Minimal stand-in for a githubkit Response."""

    def __init__(self, data: Any) -> None:
        self.parsed_data = data


def check_run(run_id: int, name: str, conclusion: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, name=name, conclusion=conclusion)


class StubClient:
    """Scriptable stand-in for the githubkit GitHub client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.repo_error: RequestFailed | None = None
        self.branch_error: RequestFailed | None = None
        self.default_branch = "main"
        self.created_pr_number = 7
        self.open_prs: list[SimpleNamespace] = []
        self.pr = SimpleNamespace(
            number=7,
            merged=False,
            state="open",
            head=SimpleNamespace(sha="abc123"),
            node_id="PR_1",
            mergeable=None,
        )
        self.check_pages: list[list[SimpleNamespace]] = [[]]
        self.reviews: list[SimpleNamespace] = []
        self.rerequest_errors: dict[int, RequestFailed] = {}
        self.merge_errors: list[RequestFailed | None] = []
        self.graphql_result: dict[str, Any] = {}
        self.graphql_error: GraphQLError | None = None
        self.graphql_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.rest = SimpleNamespace(
            repos=SimpleNamespace(get=self._repos_get, get_branch=self._repos_get_branch),
            pulls=SimpleNamespace(
                create=self._pulls_create,
                list=self._pulls_list,
                get=self._pulls_get,
                merge=self._pulls_merge,
                list_reviews=self._pulls_list_reviews,
            ),
            checks=SimpleNamespace(
                list_for_ref=self._checks_list_for_ref, rerequest_run=self._checks_rerequest_run
            ),
            issues=SimpleNamespace(create_comment=self._issues_create_comment),
        )

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.graphql_calls.append((query, variables))
        if self.graphql_error is not None:
            raise self.graphql_error
        return self.graphql_result

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    def _repos_get(self, owner: str, repo: str) -> Resp:
        self._record("repos.get", owner=owner, repo=repo)
        if self.repo_error is not None:
            raise self.repo_error
        return Resp(SimpleNamespace(default_branch=self.default_branch))

    def _repos_get_branch(self, owner: str, repo: str, branch: str) -> Resp:
        self._record("repos.get_branch", owner=owner, repo=repo, branch=branch)
        if self.branch_error is not None:
            raise self.branch_error
        return Resp(SimpleNamespace(name=branch))

    def _pulls_create(self, owner: str, repo: str, **kwargs: Any) -> Resp:
        self._record("pulls.create", owner=owner, repo=repo, **kwargs)
        return Resp(SimpleNamespace(number=self.created_pr_number))

    def _pulls_list(self, owner: str, repo: str, **kwargs: Any) -> Resp:
        self._record("pulls.list", owner=owner, repo=repo, **kwargs)
        return Resp(list(self.open_prs))

    def _pulls_get(self, owner: str, repo: str, pull_number: int) -> Resp:
        self._record("pulls.get", owner=owner, repo=repo, pull_number=pull_number)
        return Resp(self.pr)

    def _pulls_merge(self, owner: str, repo: str, pull_number: int, **kwargs: Any) -> Resp:
        self._record("pulls.merge", owner=owner, repo=repo, pull_number=pull_number, **kwargs)
        if self.merge_errors:
            error = self.merge_errors.pop(0)
            if error is not None:
                raise error
        return Resp(SimpleNamespace(merged=True))

    def _pulls_list_reviews(self, owner: str, repo: str, pull_number: int) -> Resp:
        self._record("pulls.list_reviews", owner=owner, repo=repo, pull_number=pull_number)
        return Resp(list(self.reviews))

    def _checks_list_for_ref(self, owner: str, repo: str, **kwargs: Any) -> Resp:
        self._record("checks.list_for_ref", owner=owner, repo=repo, **kwargs)
        page = kwargs.get("page", 1)
        runs = self.check_pages[page - 1] if page <= len(self.check_pages) else []
        total = sum(len(p) for p in self.check_pages)
        return Resp(SimpleNamespace(total_count=total, check_runs=list(runs)))

    def _checks_rerequest_run(self, owner: str, repo: str, check_run_id: int) -> Resp:
        self._record("checks.rerequest_run", owner=owner, repo=repo, check_run_id=check_run_id)
        if check_run_id in self.rerequest_errors:
            raise self.rerequest_errors[check_run_id]
        return Resp(SimpleNamespace())

    def _issues_create_comment(
        self, owner: str, repo: str, issue_number: int, **kwargs: Any
    ) -> Resp:
        self._record(
            "issues.create_comment", owner=owner, repo=repo, issue_number=issue_number, **kwargs
        )
        return Resp(SimpleNamespace(id=1))


@pytest.fixture
def stub() -> StubClient:
    return StubClient()


@pytest.fixture
def host(stub: StubClient) -> GitHubCodeHost:
    return GitHubCodeHost("octo/ticketflow", client=cast("GitHub[Any]", stub))


def status_graphql(
    review_decision: str | None = None, resolved: tuple[bool, ...] = ()
) -> dict[str, Any]:
    return {
        "repository": {
            "pullRequest": {
                "reviewDecision": review_decision,
                "reviewThreads": {"nodes": [{"isResolved": r} for r in resolved]},
            }
        }
    }


def feedback_graphql(threads: list[dict[str, Any]]) -> dict[str, Any]:
    return {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}


def thread(thread_id: str, resolved: bool, *comments: dict[str, Any]) -> dict[str, Any]:
    return {"id": thread_id, "isResolved": resolved, "comments": {"nodes": list(comments)}}


def comment(
    body: str,
    created_at: str = "2026-08-01T12:00:00Z",
    author: dict[str, Any] | None = None,
    path: str | None = "src/x.py",
    line: int | None = 3,
) -> dict[str, Any]:
    return {
        "id": "C_1",
        "author": author if author is not None else {"login": "reviewer"},
        "body": body,
        "path": path,
        "line": line,
        "createdAt": created_at,
    }


class TestRepoAndBranches:
    def test_satisfies_port(self, host: GitHubCodeHost) -> None:
        port: CodeHostPort = host
        assert isinstance(port, GitHubCodeHost)

    def test_repo_exists(self, host: GitHubCodeHost) -> None:
        assert host.repo_exists() is True

    def test_repo_exists_404_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.repo_error = request_failed(404)
        assert host.repo_exists() is False

    def test_repo_exists_other_errors_raise(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.repo_error = request_failed(500)
        with pytest.raises(RequestFailed):
            host.repo_exists()

    def test_default_branch(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.default_branch = "trunk"
        assert host.default_branch() == "trunk"

    def test_default_branch_none_when_repo_missing(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.repo_error = request_failed(404)
        assert host.default_branch() is None

    def test_branch_exists(self, host: GitHubCodeHost, stub: StubClient) -> None:
        assert host.branch_exists("tf/node-1") is True
        assert (
            "repos.get_branch",
            {"owner": "octo", "repo": "ticketflow", "branch": "tf/node-1"},
        ) in stub.calls

    def test_branch_exists_404_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.branch_error = request_failed(404)
        assert host.branch_exists("gone") is False

    def test_branch_exists_other_errors_raise(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.branch_error = request_failed(503)
        with pytest.raises(RequestFailed):
            host.branch_exists("any")


class TestPrLifecycle:
    def test_open_pr_targets_default_branch(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.default_branch = "trunk"
        stub.created_pr_number = 41
        assert host.open_pr("tf/node-1", "Title", "Body") == 41
        _method, kwargs = next(c for c in stub.calls if c[0] == "pulls.create")
        assert kwargs["head"] == "tf/node-1"
        assert kwargs["base"] == "trunk"
        assert kwargs["title"] == "Title"
        assert kwargs["body"] == "Body"

    def test_open_pr_missing_repo_raises(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.repo_error = request_failed(404)
        with pytest.raises(ValueError, match="octo/ticketflow"):
            host.open_pr("tf/node-1", "Title", "Body")

    def test_find_pr_for_branch(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.open_prs = [SimpleNamespace(number=5), SimpleNamespace(number=9)]
        assert host.find_pr_for_branch("tf/node-1") == 5
        _method, kwargs = next(c for c in stub.calls if c[0] == "pulls.list")
        assert kwargs["state"] == "open"
        assert kwargs["head"] == "octo:tf/node-1"

    def test_find_pr_for_branch_none(self, host: GitHubCodeHost) -> None:
        assert host.find_pr_for_branch("tf/node-1") is None

    def test_post_comment(self, host: GitHubCodeHost, stub: StubClient) -> None:
        host.post_comment(7, "handing off")
        _method, kwargs = next(c for c in stub.calls if c[0] == "issues.create_comment")
        assert kwargs["issue_number"] == 7
        assert kwargs["body"] == "handing off"


class TestGetPrStatus:
    def test_check_conclusions_map_to_canonical_states(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.check_pages = [
            [
                check_run(1, "unit", "success"),
                check_run(2, "lint", "neutral"),
                check_run(3, "docs", "skipped"),
                check_run(4, "e2e", "failure"),
                check_run(5, "slow", "timed_out"),
                check_run(6, "flaky", "cancelled"),
                check_run(7, "gate", "action_required"),
                check_run(8, "old", "stale"),
                check_run(9, "boot", None),
            ]
        ]
        stub.graphql_result = status_graphql()
        status = host.get_pr_status(7)
        states = {c.name: c.state for c in status.checks}
        assert states["unit"] is CheckState.SUCCESS
        assert states["lint"] is CheckState.SUCCESS
        assert states["docs"] is CheckState.SUCCESS
        assert states["boot"] is CheckState.PENDING
        assert status.checks_failed == ("e2e", "slow", "flaky", "gate", "old")
        assert status.checks_pending is True

    def test_all_green(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [[check_run(1, "unit", "success")]]
        stub.graphql_result = status_graphql(review_decision="APPROVED")
        status = host.get_pr_status(7)
        assert status.checks_green is True
        assert status.review_decision is ReviewDecision.APPROVED

    def test_check_runs_paginate(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [
            [check_run(1, "a", "success"), check_run(2, "b", "success")],
            [check_run(3, "c", "failure")],
        ]
        stub.graphql_result = status_graphql()
        status = host.get_pr_status(7)
        assert len(status.checks) == 3
        pages = [c[1]["page"] for c in stub.calls if c[0] == "checks.list_for_ref"]
        assert pages == [1, 2]

    def test_merged_detection(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.pr.merged = True
        stub.pr.state = "closed"
        stub.graphql_result = status_graphql()
        assert host.get_pr_status(7).state == "merged"

    def test_closed_state_passes_through(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.pr.state = "closed"
        stub.graphql_result = status_graphql()
        assert host.get_pr_status(7).state == "closed"

    def test_null_review_decision_maps_to_none(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.graphql_result = status_graphql(review_decision=None)
        assert host.get_pr_status(7).review_decision is ReviewDecision.NONE

    def test_changes_requested_and_unresolved_threads(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.graphql_result = status_graphql(
            review_decision="CHANGES_REQUESTED", resolved=(True, False, False)
        )
        status = host.get_pr_status(7)
        assert status.review_decision is ReviewDecision.CHANGES_REQUESTED
        assert status.unresolved_threads == 2

    def test_review_required_maps(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.graphql_result = status_graphql(review_decision="REVIEW_REQUIRED")
        assert host.get_pr_status(7).review_decision is ReviewDecision.REVIEW_REQUIRED


class TestGetFeedback:
    def test_unresolved_thread_comments_are_emitted(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.graphql_result = feedback_graphql(
            [
                thread("T_resolved", True, comment("old news")),
                thread(
                    "T_open", False, comment("fix this"), comment("and this", path=None, line=None)
                ),
            ]
        )
        feedback = host.get_feedback(7, since=None)
        assert [c.body for c in feedback] == ["fix this", "and this"]
        first = feedback[0]
        assert first.thread_id == "T_open"
        assert first.author == "reviewer"
        assert first.path == "src/x.py"
        assert first.line == 3
        assert first.created_at == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        assert feedback[1].path is None

    def test_since_drops_older_comments(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.graphql_result = feedback_graphql(
            [
                thread(
                    "T_1",
                    False,
                    comment("stale", created_at="2026-08-01T12:00:00Z"),
                    comment("boundary", created_at="2026-08-02T00:00:00Z"),
                    comment("fresh", created_at="2026-08-03T09:30:00Z"),
                )
            ]
        )
        since = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
        feedback = host.get_feedback(7, since=since)
        assert [c.body for c in feedback] == ["fresh"]

    def test_missing_author_becomes_empty_string(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.graphql_result = feedback_graphql(
            [thread("T_1", False, {**comment("ghost"), "author": None})]
        )
        assert host.get_feedback(7, since=None)[0].author == ""

    def test_latest_changes_requested_review_is_emitted(
        self, host: GitHubCodeHost, stub: StubClient
    ) -> None:
        stub.graphql_result = feedback_graphql([])
        stub.reviews = [
            SimpleNamespace(
                id=11,
                state="CHANGES_REQUESTED",
                body="first pass",
                user=SimpleNamespace(login="alice"),
                submitted_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            SimpleNamespace(id=12, state="APPROVED", body="lgtm", user=None, submitted_at=None),
            SimpleNamespace(
                id=13,
                state="CHANGES_REQUESTED",
                body="please split this up",
                user=SimpleNamespace(login="bob"),
                submitted_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
        ]
        feedback = host.get_feedback(7, since=None)
        assert len(feedback) == 1
        review = feedback[0]
        assert review.thread_id == "review-13"
        assert review.author == "bob"
        assert review.body == "please split this up"
        assert review.path is None
        assert review.created_at == datetime(2026, 8, 2, tzinfo=UTC)

    def test_empty_review_body_is_dropped(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.graphql_result = feedback_graphql([])
        stub.reviews = [
            SimpleNamespace(id=11, state="CHANGES_REQUESTED", body="", user=None, submitted_at=None)
        ]
        assert host.get_feedback(7, since=None) == []

    def test_review_without_user_or_timestamp(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.graphql_result = feedback_graphql([])
        stub.reviews = [
            SimpleNamespace(
                id=11, state="CHANGES_REQUESTED", body="anon", user=None, submitted_at=None
            )
        ]
        review = host.get_feedback(7, since=None)[0]
        assert review.author == ""
        assert review.created_at is None


class TestResolveThread:
    def test_resolve_thread_calls_mutation(self, host: GitHubCodeHost, stub: StubClient) -> None:
        host.resolve_thread("T_123")
        query, variables = stub.graphql_calls[0]
        assert "resolveReviewThread" in query
        assert variables == {"threadId": "T_123"}


class TestRerunFailedChecks:
    def test_reruns_only_failed_runs(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [
            [
                check_run(1, "green", "success"),
                check_run(2, "red", "failure"),
                check_run(3, "pending", None),
                check_run(4, "timed", "timed_out"),
            ]
        ]
        assert host.rerun_failed_checks(7) is True
        rerequested = [c[1]["check_run_id"] for c in stub.calls if c[0] == "checks.rerequest_run"]
        assert rerequested == [2, 4]

    def test_403_is_tolerated(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [[check_run(2, "red", "failure"), check_run(4, "timed", "timed_out")]]
        stub.rerequest_errors = {2: request_failed(403)}
        assert host.rerun_failed_checks(7) is True

    def test_all_403_returns_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [[check_run(2, "red", "failure")]]
        stub.rerequest_errors = {2: request_failed(403)}
        assert host.rerun_failed_checks(7) is False

    def test_no_failures_returns_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [[check_run(1, "green", "success")]]
        assert host.rerun_failed_checks(7) is False

    def test_other_errors_raise(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.check_pages = [[check_run(2, "red", "failure")]]
        stub.rerequest_errors = {2: request_failed(500)}
        with pytest.raises(RequestFailed):
            host.rerun_failed_checks(7)


class TestMerge:
    def test_squash_merge_succeeds(self, host: GitHubCodeHost, stub: StubClient) -> None:
        assert host.merge(7) is True
        _method, kwargs = next(c for c in stub.calls if c[0] == "pulls.merge")
        assert kwargs["merge_method"] == "squash"

    def test_405_falls_back_to_merge_commit(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(405), None]
        assert host.merge(7) is True
        methods = [c[1]["merge_method"] for c in stub.calls if c[0] == "pulls.merge"]
        assert methods == ["squash", "merge"]

    def test_405_then_405_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(405), request_failed(405)]
        assert host.merge(7) is False

    def test_405_then_409_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(405), request_failed(409)]
        assert host.merge(7) is False

    def test_409_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(409)]
        assert host.merge(7) is False

    def test_other_errors_raise(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(500)]
        with pytest.raises(RequestFailed):
            host.merge(7)

    def test_fallback_other_errors_raise(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.merge_errors = [request_failed(405), request_failed(502)]
        with pytest.raises(RequestFailed):
            host.merge(7)


class TestEnableAutoMerge:
    def test_enable_auto_merge(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.pr.node_id = "PR_node42"
        assert host.enable_auto_merge(7) is True
        query, variables = stub.graphql_calls[0]
        assert "enablePullRequestAutoMerge" in query
        assert variables == {"pullRequestId": "PR_node42"}

    def test_graphql_error_is_false(self, host: GitHubCodeHost, stub: StubClient) -> None:
        stub.graphql_error = GraphQLError("auto-merge is not allowed on this repository")
        assert host.enable_auto_merge(7) is False


class TestMergeable:
    def test_conflict_signal_passes_through(self, stub: StubClient, host: GitHubCodeHost) -> None:
        stub.pr.mergeable = False
        stub.graphql_result = status_graphql()
        assert host.get_pr_status(7).mergeable is False

    def test_unknown_while_recomputing(self, stub: StubClient, host: GitHubCodeHost) -> None:
        stub.graphql_result = status_graphql()
        assert host.get_pr_status(7).mergeable is None
