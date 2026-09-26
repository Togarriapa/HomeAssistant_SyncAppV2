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
- content-free rollback aggregates for every phase, reconciliation state and block
  reason, plus total/maximum rollback attempts and the latest update timestamp.

Every known status is represented even when its count is zero, and an empty ledger
produces an explicit empty summary. Candidate entries are status information only;
no candidate SHA or retry capability is published.

Rollback evidence is read through a second bounded StateStore projection that never
selects deployment IDs, repository targets, baseline/candidate SHAs, backup slugs,
Supervisor job UUIDs or record digests. It contains only phase, reconciliation state,
block reason, attempt count and update time. Tokens, paths, response bodies and nested
exception text are likewise absent.

## Safety boundary

Runtime generation does not recover, claim, requeue, unblock or complete work. It
does not grant retry authority and cannot bypass configuration validation, backup,
Apply, deployment observation or rollback. A collection or merge failure follows
the existing transient failure path for the already-claimed runtime-generation
item; it does not change any other work item. Automatic Retrigger handling continues
to exclude deterministic blocked work.
