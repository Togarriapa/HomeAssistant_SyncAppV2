# Retrigger runtime status

The initial V2 README requires every Retrigger execution, retry, skip and failure to
be available for troubleshooting and AI analysis. Runtime inventory now includes a
bounded `analysis/recovery.json` summary derived from the durable work ledger owned
by the SyncApp service.

## Evidence boundary

`StateStore.recovery_work_evidence()` performs one read-only query and deliberately
does not select `work_key`. The resulting evidence contains only validated work
kind, lifecycle status, attempt count and lifecycle timestamps. Repository targets,
candidate commit identities, local paths, tokens, exception text and raw work keys
cannot enter the runtime artifact through this interface.

The query and pure renderer both enforce a maximum of 4,096 rows. Malformed,
oversized or temporally inconsistent evidence fails closed instead of producing a
partial or misleading status file. The renderer receives an explicit UTC reference
time and never reads the wall clock, so identical evidence and reference time
produce identical canonical output and artifact identity.

## Published summary

The recovery file exposes:

- total work and counts for `pending`, `running`, `retry`, `blocked` and
  `succeeded` states;
- bounded total and maximum attempt counts;
- ready work and scheduled-backoff counts with the earliest next-attempt time;
- the same aggregates per validated work kind, in deterministic order.
- `candidate_apply` admission work as those same content-free lifecycle, attempt,
  readiness and backoff aggregates; deployment IDs and candidate SHAs remain omitted.
- `candidate_apply_execute` live Apply progress as the same content-free work
  aggregates, without deployment IDs, paths, operation indexes or commit identities.
- `candidate_restart` execution progress as the same content-free work aggregates,
  without deployment IDs, authorization digests, credentials or request evidence.
- `candidate_observe` Core health-window progress as the same content-free aggregates,
  without deployment IDs, deadlines, health digests, credentials or response content.
- `candidate_observe_supervisor` health progress as the same content-free aggregates,
  without deployment IDs, evidence digests, credentials or Supervisor response data.
- content-free rollback aggregates for every phase, reconciliation state and block
  reason, plus total/maximum rollback attempts and the latest update timestamp.
- content-free candidate Fetch/Stage phase counts, aggregate staged entry/byte
  counts and the latest checkpoint timestamp.
- `administrative_retry_requests` with the total number processed, fixed
  `retried` and `rejected` outcome counts, and the latest processed UTC timestamp.

Every known status is represented even when its count is zero, and an empty ledger
produces an explicit empty summary. Candidate entries are status information only;
no candidate SHA or retry capability is published.

Administrative retry outcome evidence is a bounded read-only projection. Its query
selects only the fixed outcome and processing timestamp; request IDs, work kinds,
work keys, identity digests, receipt digests and raw App options are never selected.
Malformed, future-dated or oversized evidence fails closed. These aggregates confirm
that explicit requests were consumed or rejected, but they cannot identify, rearm,
claim, unblock or otherwise mutate any work item and grant no retry authority.

A successful `candidate_apply` item means only that the complete admission chain was
freshly re-proven and an immutable live Apply intent was durably recorded. It does not
mean that a live file operation, reload, restart, observation, promotion or rollback
was performed. Retry and blocked counts preserve the distinction between transient
proof failures and deterministic rejection without publishing nested error text.

A successful `candidate_apply_execute` item means that the exact ordered Apply plan
reached durable completion and, for a non-empty plan, activation authority was
persisted before restart work was scheduled. It does not mean restart, observation,
promotion, or rollback has occurred.

A successful `candidate_restart` item means only that the exact authorized restart
request was durably acknowledged and its observation successor was atomically
scheduled. A blocked item can represent an uncertain request outcome or invalid
authority; runtime status does not expose which deployment, token, digest, response,
or nested failure produced that aggregate.

A successful `candidate_observe` item means only that exact Core API health was proved
at both ends of the configured interval and the Supervisor-observation successor was
atomically scheduled. It does not mean that Supervisor, integrations, resources,
entities, automations, assertions, promotion, or rollback have been evaluated.

A successful `candidate_observe_supervisor` item means only that exact healthy and
supported Supervisor evidence was persisted and integration observation was scheduled.
It grants no finalization, promotion, restore, or rollback authority.

A successful `candidate_observe_integrations` item means only that every enabled
config entry reported exact state `loaded`, disabled entries were safely excluded,
and startup-error observation was scheduled. Runtime output includes only aggregate
work status, attempts, readiness, and backoff; it excludes deployment and config-entry
identities, credentials, response content, and nested errors. This evidence grants no
finalization, promotion, restore, rollback, or mutation authority.

A successful `candidate_observe_startup_errors` item means only that a durable clear
or significant-error outcome was bound to the exact prerequisite chain and the
corresponding resource-observation or finalization successor was atomically scheduled.
Runtime output exposes only aggregate work status, attempts, readiness, and backoff;
it excludes deployment identities, log messages, logger names, paths, credentials,
response content, evidence digests, and nested errors. This read-only stage grants no
direct mutation, promotion, restore, or rollback authority.

A successful `candidate_observe_resources` item means only that exact derived-resource
availability or missing-resource evidence was persisted and its corresponding
entity-observation or finalization successor was atomically scheduled. Runtime output
contains only aggregate work status, attempts, readiness, and backoff. It excludes
deployment and entity identities, states, attributes, candidate evidence, credentials,
response content, paths, and nested errors. The observation itself grants no mutation,
promotion, restore, or rollback authority.

A successful `candidate_observe_entities` item means only that content-free
valid/invalid aggregate evidence was persisted and the exact authorized
automation/script-observation or finalization successor was scheduled atomically.

`candidate_observe_automation_scripts` is projected through the same bounded
work counters and pending/ready aggregates. Its successful state means only
that content-free load evidence was persisted and exactly one assertion or
finalization successor was scheduled atomically. The projection exposes no
deployment identity, entity ID, state, token, response body, digest, or mutation
authority.

`candidate_observe_assertions` is projected through the same bounded work
counters and pending/ready aggregates. Success means only that content-free
assertion counts were persisted and exact finalization work was scheduled. The
projection excludes deployment identity, entity IDs, states, assertion JSON,
credentials, response content, digests, and every promotion or rollback
decision.
Runtime output contains aggregate work status, attempts, readiness, and backoff
only. It excludes deployment/entity identities, state values, attributes,
credentials, response content, evidence digests, paths, and nested errors. This
read-only stage grants no mutation, promotion, restore, or rollback authority.

`candidate_finalize` is exposed through the same aggregate-only work projection.
Success means only that immutable authority was persisted and exactly one inert
promotion or rollback successor was scheduled. Deployment identity, candidate
SHA, backup slug, evidence digests, terminal payloads, credentials, and nested
errors are never selected into runtime output.

`candidate_promote` is exposed through the same aggregate-only work projection.
Its success means only that durable promotion evidence reached `completed` and
the exact work item finished. Repository identity, candidate and baseline SHAs,
known-good tag, backup slug, finalization digest, credential, remote response,
and nested error details are excluded.

`candidate_rollback` is exposed through the same aggregate-only work projection.
Its success means only that immutable rollback intent and recovery authority were
persisted and the exact authorization handoff finished. It does not mean a restore
was requested or completed. Deployment identity, repository and backup identity,
candidate and baseline SHAs, finalization and authority digests, credentials,
remote responses, and nested error details are excluded.

Rollback evidence is read through a second bounded StateStore projection that never
selects deployment IDs, repository targets, baseline/candidate SHAs, backup slugs,
Supervisor job UUIDs or record digests. It contains only phase, reconciliation state,
block reason, attempt count and update time. Tokens, paths, response bodies and nested
exception text are likewise absent.

Fetch/Stage checkpoint evidence is reconstructed from integrity-protected schema-v27
rows. Its runtime projection omits candidate SHA, repository target and ID, workspace
identity/path, manifest digest and content, credentials and nested exception text.

## Safety boundary

Runtime generation does not recover, claim, requeue, unblock or complete work. It
does not grant retry authority and cannot bypass configuration validation, backup,
Apply, deployment observation or rollback. A collection or merge failure follows
the existing transient failure path for the already-claimed runtime-generation
item; it does not change any other work item. Automatic Retrigger handling continues
to exclude deterministic blocked work.
