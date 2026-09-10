# Explicit Administrative Work Retry

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, specifically the Retrigger and Recovery requirement that deterministic failures remain blocked unless the work identity changes or an administrator explicitly retries them.

`retry_blocked_work()` is deliberately separate from automatic Retrigger processing. It accepts exactly one validated durable `(work_kind, work_key)` identity and will only transition that exact item from `blocked` to `pending`.

An explicit retry starts a new bounded retry budget by resetting the attempt counter to zero and making the item eligible at the supplied administrative retry timestamp. The original `created_at` value is preserved so the durable identity and history of the work item are not replaced or disguised as newly discovered work.

The transition is atomic and fails closed if the item is missing, is not blocked, changes concurrently, contains an invalid identity, or cannot be verified after the update. There is no wildcard, bulk unblock, automatic unblock, or change to normal exponential-backoff behavior.

This primitive does not itself expose a network/UI/CLI administrative endpoint. A future administrative surface may call it only after establishing its own authentication, authorization and audit boundary.

This slice does not change candidate validation, backup, Apply, reload/restart, observation, rollback, Git transport, credentials, Retrigger scheduling, or the live Home Assistant configuration tree.
