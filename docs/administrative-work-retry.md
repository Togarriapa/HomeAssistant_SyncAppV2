# Explicit Administrative Work Retry

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, specifically the Retrigger and Recovery requirement that deterministic failures remain blocked unless the work identity changes or an administrator explicitly retries them.

`retry_blocked_work()` is deliberately separate from automatic Retrigger processing. It accepts exactly one validated durable `(work_kind, work_key)` identity and will only transition that exact item from `blocked` to `pending`.

An explicit retry starts a new bounded retry budget by resetting the attempt counter to zero and making the item eligible at the supplied administrative retry timestamp. The original `created_at` value is preserved so the durable identity and history of the work item are not replaced or disguised as newly discovered work.

The transition is atomic and fails closed if the item is missing, is not blocked, changes concurrently, contains an invalid identity, or cannot be verified after the update. There is no wildcard, bulk unblock, automatic unblock, or change to normal exponential-backoff behavior.

The installed App exposes this boundary through three all-or-none Supervisor App
options: a canonical UUIDv4 request ID, exact work kind and exact work key. The
Supervisor option boundary is the operator action; no unauthenticated network
endpoint is added. The work key uses the `password?` schema so the UI masks it, and
neither raw identity is copied into logs or the durable request receipt.

Schema v35 stores a content-free receipt for every request ID. The receipt binds the
UUID to a SHA-256 digest of the exact work identity plus a fixed `retried` or
`rejected` outcome, UTC timestamp and integrity digest. The raw work kind and key
are not retained in that table. Receipt insertion and a successful blocked-to-
pending transition share one `BEGIN IMMEDIATE` transaction.

The same request UUID and identity replays without changing work. Reusing a UUID
for another identity fails closed. Missing and non-blocked targets are durably
rejected, so persistent App options cannot create a restart loop; another attempt
requires a new UUID. Startup processes this request before repository trust and
normal work producers, then emits only a fixed `completed`, `rejected` or `skipped`
event.

This surface does not change candidate validation, backup, Apply, reload/restart,
observation, rollback, Git transport, credentials, Retrigger scheduling, or the
live Home Assistant configuration tree.
