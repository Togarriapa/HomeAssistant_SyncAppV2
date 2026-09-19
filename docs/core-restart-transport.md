# Crash-safe Home Assistant Core restart transport

`request_core_restart_once()` is the bounded mutation boundary that consumes an exact
durable post-Apply activation authorization and requests one Home Assistant Core
restart. It does not infer eligibility: it reloads and revalidates the authorization
before recording or sending anything.

## Supervisor contract

The transport uses the Supervisor Core API contract implemented by Home Assistant:

- `POST http://supervisor/core/restart`
- bearer authentication from `SUPERVISOR_TOKEN`
- JSON body `{"force":false,"safe_mode":false}`
- a bounded timeout and response body
- exact successful response `{"result":"ok","data":{}}`

Credentials and response details are never persisted or included in failures. Non-200,
non-JSON, oversized, malformed, duplicate-key, or non-success responses fail with a
sanitized uncertain-outcome error.

## Journal-before-mutation ordering

Schema version 12 adds one content-free restart-attempt record per deployment. The
record is bound to the exact activation-authorization digest and is written as
`request_started` in an immediate transaction before the Supervisor request can run.
The response may advance it to `request_acknowledged` only after the exact success
envelope is received.

Both phases are integrity-digested and revalidated on every read. A transaction-time
recheck closes the race between initial discovery and journal creation. An existing
record never causes another request:

- `request_acknowledged` is an idempotent replay;
- `request_started` requires later reconciliation and is not retried blindly.

Acknowledgement proves only that Supervisor accepted the request. It is not evidence
that Core restarted, became healthy, or loaded the candidate successfully. The
subsequent bounded API-root proof is documented in
[`core-health-observation.md`](core-health-observation.md). A crash,
timeout, connection failure, invalid response, or interruption after the request leaves
`request_started`, because the mutation outcome is uncertain.

## Authority boundary

This slice performs at most one authorized Supervisor restart request. It does not
reconcile uncertain restart outcomes, promote or reject a candidate, update Git, restore
a backup, or roll back. Those later decisions must use their own durable, read-only
evidence and must never convert uncertainty into a repeated restart.
