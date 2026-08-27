# ADR-0013: Operational policies: yolo mode, spend, retention

- Status: Accepted
- Date: 2026-08-27

## Context

ticketflow runs coding agents unattended, sometimes for days, which raises three
operational questions the spec answers deliberately narrowly. First, how much
human ceremony an operator may remove: `--yolo` exists for runs where the
operator accepts agent autonomy, but it must not become a back door around the
repo's gates, which are host-enforced and outside ticketflow's power to bypass
(spec §14, §1.1, ADR-0009). Second, money: model spend is already capped at the
provider account, and duplicating budget controls in the orchestrator would mean
two places to set a cap and one of them wrong (spec §12.3). What the
orchestrator does need is protection against runaway attempts and against a
provider-side quota wall failing every in-flight node at once.

Third, what survives a run: agent logs are almost all the bytes while briefs,
plans and handoffs are kilobytes, so retention splits by class (spec §12.7), and
downstream nodes consume their direct upstream context through a small, bounded
handoff artifact rather than transitive history (spec §12.2).

## Decision

**Yolo mode.** `--yolo` does exactly two things and nothing else:

- Auto-approve the plan, when the planner exists (ADR-0014): the review session
  of spec §13.5 is skipped entirely.
- Run agents without permission prompts: no `ToolPolicy` is consulted, and the
  agent is never asked to confirm a tool call.

It cannot touch the repo's gates. Branch protection, required checks and
required reviewers are enforced server-side by the host; ticketflow has no power
to bypass them under any flag (ADR-0009). The merge ladder asks the same
question under `--yolo` and gets the same answer.

- Print one warning at startup, then nothing. Do not repeat it per PR or per
  event, and warn rather than refuse even when the target is the default branch.
- Treat the flag as per-run: never persist it, and never inherit it into a
  resumed run.
- Write every artifact regardless of the flag — plan file, research briefs,
  handoffs, event log. The event log records the flag as a run-level fact.

**Spend.** ticketflow implements no budget management. Spend limits belong to
the provider account and the operator's API keys. Two mechanisms remain, and
neither is budgeting:

- The per-attempt runaway guard (owned by ADR-0010): a hard wall-clock and
  token ceiling that terminates a stuck loop. It is the same category as the
  retry cap — it kills a loop, it does not manage spend.
- Cost recording: real cost per attempt goes to the event log (ADR-0005) as
  telemetry. Normalization at the adapter boundary and the distinct
  quota/rate-limit error class (pause dispatch, resume via intent) are
  adapter obligations owned by ADR-0011.

**Handoffs (spec §12.2).** Each node's final action writes `handoff.md`, capped
at roughly 300 words: files touched, interfaces introduced or changed, decisions
made and why, deliberate omissions, and known gotchas. Store it in SQLite keyed
by node (ADR-0003) and post it as a PR comment so humans see it too. Downstream
prompts receive direct upstream handoffs only — never transitive.

**Retention.** Split by artifact class, with liveness beating age. The policy
rationale is that agent logs are almost all the bytes while the small artifacts
(briefs, plans, handoffs) are the valuable ones. The concrete classes, caps and
enforcement are owned by ADR-0010; this ADR adds nothing normative to them.

## Consequences

- Easier: unattended runs on well-gated repos. `--yolo` removes only
  ticketflow's own prompts, so the repo's gates carry the full quality burden —
  which makes the mode more dependent on those gates, not less, and makes weak
  repos genuinely risky under the flag. That risk is stated, not mitigated.
- Easier: cost analysis. Normalized per-attempt cost in the event log reveals
  which ticket shapes are expensive, without any enforcement machinery to
  maintain.
- Harder: operators who want a spend ceiling must configure it at the provider;
  ticketflow will not stop a run for costing too much, only for looping too
  long. This is deliberate and deferred indefinitely.
- Harder: quota exhaustion recovery is manual by design — dispatch stays paused
  until a human writes a resume intent.
- The one-warning policy trades repeated reminders for signal: the run record
  and the event log's run-level yolo fact are the only review an unattended run
  gets.
- Handoff quality is unverified: a node that writes a poor handoff degrades its
  dependents' context, and nothing checks the ~300-word artifact beyond its
  existence.

## Review guidance

- Flag any code path where `--yolo` influences merge behaviour, gate
  evaluation, or the §9.1 merge ladder — the flag may only skip plan approval
  and `ToolPolicy` prompts.
- Flag persistence of the yolo flag: writes of it to SQLite config, run
  resumption code that carries it forward, or defaults sourced from a previous
  run.
- Flag any artifact write (plan file, brief, handoff, event log) made
  conditional on the yolo flag.
- Flag budget, spend-cap, or cost-limit logic anywhere in ticketflow
  (attempt-terminating resource ceilings are the runaway guard, owned and
  enforced by ADR-0010; cost normalization and quota classification by
  ADR-0011).
- Require handoff plumbing to pass direct upstream handoffs only; flag any
  transitive traversal collecting grandparent handoffs into a prompt.
