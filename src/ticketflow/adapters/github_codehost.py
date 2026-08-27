"""GitHub code host adapter implementing CodeHostPort (ADR-0002).

Translates githubkit responses into the canonical DTOs of
``ticketflow.ports.codehost`` at this edge: no vendor type crosses the port
boundary, and no scheduling, retry, or state-transition decision lives here —
adapters translate, they never decide (ADR-0002). What the merge ladder does
with these signals is the orchestrator's business (ADR-0009).

The vendor client is an injectable seam: tests pass a stub object in place of
the ``GitHub`` client, so no test touches the network. Every vendor call sits
behind a small private method to keep that seam thin. githubkit is pinned
exactly (ADR-0012), so an SDK upgrade touches this file alone.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from githubkit import GitHub
from githubkit.exception import GraphQLError, RequestFailed

from ticketflow.ports.codehost import (
    CheckConclusion,
    CheckState,
    PrStatus,
    ReviewComment,
    ReviewDecision,
)

if TYPE_CHECKING:
    from githubkit_schemas.latest.models import (
        CheckRun,
        FullRepository,
        PullRequest,
        PullRequestReview,
        PullRequestSimple,
    )

_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
"""Check-run conclusions the merge ladder treats as green (ADR-0009)."""

_REVIEW_DECISIONS = {
    "APPROVED": ReviewDecision.APPROVED,
    "CHANGES_REQUESTED": ReviewDecision.CHANGES_REQUESTED,
    "REVIEW_REQUIRED": ReviewDecision.REVIEW_REQUIRED,
}

_PR_REVIEW_QUERY = """
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewDecision
      reviewThreads(first: 100) {
        nodes { isResolved }
      }
    }
  }
}
"""

_FEEDBACK_QUERY = """
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 50) {
            nodes { id author { login } body path line createdAt }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation ($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id }
  }
}
"""

_ENABLE_AUTO_MERGE_MUTATION = """
mutation ($pullRequestId: ID!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: SQUASH}) {
    pullRequest { number }
  }
}
"""


def _check_state(conclusion: str | None) -> CheckState:
    """Map a check-run conclusion onto the canonical CheckState.

    ``None`` means the run is queued or in progress. Anything not explicitly
    green (failure, timed_out, cancelled, action_required, stale, ...) is a
    failure: the merge ladder consumes conclusions as opaque signals and never
    softens them (ADR-0009).
    """
    if conclusion is None:
        return CheckState.PENDING
    if conclusion in _SUCCESS_CONCLUSIONS:
        return CheckState.SUCCESS
    return CheckState.FAILURE


class GitHubCodeHost:
    """CodeHostPort adapter for GitHub PRs and checks (ADR-0002)."""

    def __init__(self, repo: str, token: str = "", client: GitHub[Any] | None = None) -> None:
        self._repo = repo
        self._owner, self._name = repo.split("/", 1)
        self._gh = client if client is not None else (GitHub(token) if token else GitHub())

    # -- port methods ------------------------------------------------------

    def repo_exists(self) -> bool:
        return self._fetch_repo() is not None

    def default_branch(self) -> str | None:
        repo = self._fetch_repo()
        return repo.default_branch if repo is not None else None

    def branch_exists(self, branch: str) -> bool:
        try:
            self._fetch_branch(branch)
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return False
            raise
        return True

    def open_pr(self, branch: str, title: str, body: str) -> int:
        base = self.default_branch()
        if base is None:
            raise ValueError(f"repository {self._repo} does not exist")
        return self._create_pr(head=branch, base=base, title=title, body=body).number

    def find_pr_for_branch(self, branch: str) -> int | None:
        prs = self._list_open_prs(head=f"{self._owner}:{branch}")
        return prs[0].number if prs else None

    def get_pr_status(self, pr_number: int) -> PrStatus:
        pr = self._fetch_pr(pr_number)
        checks = tuple(
            CheckConclusion(name=run.name, state=_check_state(run.conclusion))
            for run in self._list_check_runs(pr.head.sha)
        )
        data = self._graphql(_PR_REVIEW_QUERY, self._pr_variables(pr_number))
        pull = data["repository"]["pullRequest"]
        threads = pull["reviewThreads"]["nodes"]
        return PrStatus(
            number=pr_number,
            state="merged" if pr.merged else pr.state,
            checks=checks,
            review_decision=_REVIEW_DECISIONS.get(
                pull["reviewDecision"] or "", ReviewDecision.NONE
            ),
            unresolved_threads=sum(1 for thread in threads if not thread["isResolved"]),
            # REST reports None while GitHub recomputes mergeability; False
            # is the conflict signal the settle ladder consumes (spec §12.1).
            mergeable=pr.mergeable,
        )

    def get_feedback(self, pr_number: int, since: datetime | None) -> list[ReviewComment]:
        data = self._graphql(_FEEDBACK_QUERY, self._pr_variables(pr_number))
        threads = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        comments: list[ReviewComment] = []
        for thread in threads:
            if thread["isResolved"]:
                continue
            for comment in thread["comments"]["nodes"]:
                created_at = datetime.fromisoformat(comment["createdAt"])
                if since is not None and created_at <= since:
                    continue
                author = comment.get("author") or {}
                comments.append(
                    ReviewComment(
                        thread_id=thread["id"],
                        author=author.get("login", ""),
                        body=comment["body"],
                        path=comment.get("path"),
                        line=comment.get("line"),
                        created_at=created_at,
                    )
                )
        review = self._latest_changes_requested_review(pr_number)
        if review is not None and review.body:
            submitted = review.submitted_at
            comments.append(
                ReviewComment(
                    thread_id=f"review-{review.id}",
                    author=review.user.login if review.user is not None else "",
                    body=review.body,
                    path=None,
                    created_at=submitted if isinstance(submitted, datetime) else None,
                )
            )
        return comments

    def resolve_thread(self, thread_id: str) -> None:
        self._graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})

    def rerun_failed_checks(self, pr_number: int) -> bool:
        """Re-request every failed check run once for flake handling (ADR-0009).

        A 403 is tolerated per run — some GitHub Apps forbid re-requests of
        their own runs — so one forbidding app does not mask the rest.
        """
        pr = self._fetch_pr(pr_number)
        rerequested = False
        for run in self._list_check_runs(pr.head.sha):
            if _check_state(run.conclusion) is not CheckState.FAILURE:
                continue
            try:
                self._rerequest_run(run.id)
            except RequestFailed as exc:
                if exc.response.status_code == 403:
                    continue
                raise
            rerequested = True
        return rerequested

    def merge(self, pr_number: int) -> bool:
        """Squash-merge; fall back to a merge commit where squash is forbidden.

        405 (method not allowed) and 409 (head mismatch/conflict) translate to
        ``False`` — not-mergeable is a signal for the caller, never retried
        here (ADR-0002).
        """
        try:
            self._merge_pr(pr_number, method="squash")
        except RequestFailed as exc:
            status = exc.response.status_code
            if status == 405:
                return self._merge_with_merge_commit(pr_number)
            if status == 409:
                return False
            raise
        return True

    def enable_auto_merge(self, pr_number: int) -> bool:
        node_id = self._fetch_pr(pr_number).node_id
        try:
            self._graphql(_ENABLE_AUTO_MERGE_MUTATION, {"pullRequestId": node_id})
        except GraphQLError:
            # Auto-merge disabled on the repo, or the PR is not eligible: the
            # caller falls back to polling the merge ladder (ADR-0009).
            return False
        return True

    def post_comment(self, pr_number: int, text: str) -> None:
        self._create_comment(pr_number, text)

    # -- private helpers ---------------------------------------------------

    def _merge_with_merge_commit(self, pr_number: int) -> bool:
        try:
            self._merge_pr(pr_number, method="merge")
        except RequestFailed as exc:
            if exc.response.status_code in (405, 409):
                return False
            raise
        return True

    def _latest_changes_requested_review(self, pr_number: int) -> PullRequestReview | None:
        latest: PullRequestReview | None = None
        for review in self._list_reviews(pr_number):
            if review.state == "CHANGES_REQUESTED":
                latest = review
        return latest

    def _pr_variables(self, pr_number: int) -> dict[str, Any]:
        return {"owner": self._owner, "name": self._name, "number": pr_number}

    # -- vendor calls (the thin seam; one githubkit call per method) -------

    def _fetch_repo(self) -> FullRepository | None:
        try:
            return self._gh.rest.repos.get(self._owner, self._name).parsed_data
        except RequestFailed as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _fetch_branch(self, branch: str) -> None:
        self._gh.rest.repos.get_branch(self._owner, self._name, branch)

    def _create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        return self._gh.rest.pulls.create(
            self._owner, self._name, head=head, base=base, title=title, body=body
        ).parsed_data

    def _list_open_prs(self, *, head: str) -> list[PullRequestSimple]:
        return self._gh.rest.pulls.list(
            self._owner, self._name, state="open", head=head
        ).parsed_data

    def _fetch_pr(self, pr_number: int) -> PullRequest:
        return self._gh.rest.pulls.get(self._owner, self._name, pr_number).parsed_data

    def _list_check_runs(self, sha: str) -> list[CheckRun]:
        runs: list[CheckRun] = []
        page = 1
        while True:
            data = self._gh.rest.checks.list_for_ref(
                self._owner, self._name, ref=sha, per_page=100, page=page
            ).parsed_data
            runs.extend(data.check_runs)
            if len(runs) >= data.total_count or not data.check_runs:
                return runs
            page += 1

    def _list_reviews(self, pr_number: int) -> list[PullRequestReview]:
        return self._gh.rest.pulls.list_reviews(self._owner, self._name, pr_number).parsed_data

    def _rerequest_run(self, check_run_id: int) -> None:
        self._gh.rest.checks.rerequest_run(self._owner, self._name, check_run_id)

    def _merge_pr(self, pr_number: int, *, method: Literal["merge", "squash"]) -> None:
        self._gh.rest.pulls.merge(self._owner, self._name, pr_number, merge_method=method)

    def _create_comment(self, pr_number: int, text: str) -> None:
        self._gh.rest.issues.create_comment(self._owner, self._name, pr_number, body=text)

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self._gh.graphql(query, variables)
