# Reference workflow templates

Illustrative, unsupported templates for the gate-weakening checks described in
[REPO_REQUIREMENTS.md](../REPO_REQUIREMENTS.md). Copy them into **your** repo's
`.github/workflows/`, adapt them to your languages and tools, and own them from
then on.

These run in the target repo's CI, not in ticketflow. ticketflow does not import
them, require them, or know whether they exist (docs/adrs/0009).

| Template | Catches |
|---|---|
| `workflows/suppression-scan.yml` | New `# noqa`, `eslint-disable`, `@ts-ignore`, ignore-file entries |
| `workflows/coverage-ratchet.yml` | Changed lines that arrive untested |
| `workflows/test-skip-ratchet.yml` | Deleted tests, new skip/xfail markers |
| `workflows/policy-reviewer.yml` | Gate weakening a grep cannot see (LLM reviewer) |

Make whichever you adopt **required status checks** on the protected branch, and
cover their configuration with CODEOWNERS so a PR cannot quietly weaken them.
Apply them to every PR, human or agent.
