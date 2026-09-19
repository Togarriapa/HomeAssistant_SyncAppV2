# Bounded post-restart Core API health proof

`observe_core_api_once()` establishes one narrow fact after an acknowledged Home
Assistant Core restart: the authenticated Core API root returned its exact healthy
payload. The proof is read-only and cannot authorize or repeat a restart.

## Probe contract

The probe is eligible only when the deployment has a valid durable restart attempt in
`request_acknowledged`. It performs at most one bounded request:

- `GET http://supervisor/core/api/`
- bearer authentication from `SUPERVISOR_TOKEN`
- a 10-second default timeout and 16-KiB default response limit
- JSON media type and exact payload `{"message":"API running."}`

Non-200, non-JSON, oversized, malformed, duplicate-key, or different payloads do not
produce healthy evidence. Transport and response details are reduced to a sanitized
failure, and credentials and response content are never persisted.

## Durable replay semantics

Schema version 13 adds one content-free `core_health_observation` per deployment. The
record stores only the deployment identity, exact acknowledged restart-attempt digest,
UTC observation time, and its own integrity digest. The restart binding and record
digest are revalidated on every read and again inside the persistence transaction.

After the exact response is validated, the observation is atomically inserted. A valid
existing observation is an idempotent result and is returned before credential
resolution or network access. Missing or unhealthy observations may be probed on a
later controlled invocation; corrupt, rebound, or unacknowledged state fails closed.

## Authority boundary

This proof means only that Core's API root answered after the exact acknowledged restart.
It does not establish an observation window, validate runtime assertions or logs,
promote or reject the candidate, reconcile an uncertain restart, restore a backup, or
roll back. The subsequent two-point API availability interval is documented in
[`core-health-window.md`](core-health-window.md). Other decisions require their own
durable evidence and safeguards.
