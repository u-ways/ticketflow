# What a well-run target repo provides

**ticketflow adds no safety of its own. It inherits yours.**

A repo that is safe to hand to an autonomous agent is a repo that was already well
run. None of the work described here is AI-specific overhead: it is ordinary
engineering rigour — the same branch protection, required checks and honest tests
that kept humans from merging broken code. Agents are not a new category of author.
A team that has done this work is ready today; a team that hasn't should do the
work, not buy a compensating control.

Doing it buys you autonomy you cannot get any other way. With strong gates you can
start an epic and leave it for days, because every merge had to satisfy rules you
wrote. With weak gates you should watch it, because the same loop will merge
whatever passes — which, on a repo with no checks, is everything.

**Nothing below is verified or enforced by ticketflow.** It runs against whatever
repo it is pointed at, asks the host one question — *can this pull request be
merged?* — and acts on the answer. These are recommendations we publish, not
preconditions we check.

## Recommendations

| # | Recommendation |
|---|---|
| R1 | Branch protection on the target branch, with required status checks. |
| R2 | Checks that detect gate *weakening*, not just gate failure (below). |
| R3 | CODEOWNERS covering CI workflow, lint, scanner and coverage configuration. |
| R4 | Required approvals ≥ 1, satisfiable by a reviewer the agent cannot act as. |
| R5 | Merge queue enabled, with "require branches to be up to date". |
| R6 | Agent identities have no admin rights and cannot alter branch protection. |

R3 and R6 together are what make the scheme hold. Note the useful asymmetry:
**deleting a required check does not help an agent**, because the host blocks merge
when a required check never reports. The only viable attack is weakening a gate in
place — which is exactly what CODEOWNERS covers.

## Detecting gate weakening

The dominant practical failure of "make CI green" as a goal is that an agent makes
CI green by weakening CI: deleting the failing test, loosening an assertion, adding
`# noqa`, suppressing a scanner finding, marking a test flaky. Each is locally
reasonable from inside the agent's context window; each produces a green PR and a
worse codebase.

Detection is repo-specific and belongs next to the code, owned by the team who can
judge whether a given weakening was legitimate. Techniques that work:

- **Diff-scoped coverage ratchet** — coverage on changed lines, compared to base.
- **Suppression scan** — fail when the diff introduces `# noqa`, `eslint-disable`,
  `@ts-ignore`, scanner ignore entries or baseline additions not present in base.
- **Test-count / skip ratchet** — fail on deleted tests or newly added skip and
  xfail markers.
- **Baseline-diffed static analysis** — report only findings new since the base
  commit.
- **A policy reviewer** — an LLM reviewer whose only question is "does this change
  reduce the strength of our checks, and where". Distinct from a general code
  reviewer, and **on a different model or provider to the worker** — otherwise
  correlated blindness returns.

Reference workflow templates for these live in [`examples/`](examples/). They are
illustrative and unsupported: your repo owns them, tunes them, and maintains them.

## One strong piece of advice

**Apply the gates to every PR, human or agent.** A gate that only applies to one
author class teaches people which author class to use.

## Bootstrap

A repo need not exist yet. "Create the repo", "add the CI pipeline", "turn on
branch protection" are perfectly good early nodes, and a graph that starts with
them builds its own safety system before the work that needs it. Early bootstrap
nodes run with almost no gates because there are none yet — they are small, they
are first, and a human is watching a brand-new project. By the time the graph
reaches work that matters, the gates it created are the ones judging it.
