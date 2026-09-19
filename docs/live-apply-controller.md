# Bounded live Apply controller

`ha_syncapp.live_apply_controller.advance_live_apply_once()` owns one bounded
decision in the live Apply transaction. It accepts the complete producer-issued
authorization, Stage proof, immutable Stage, deterministic plan, live
preconditions, and the process-exclusive `StateStore`. It re-derives the intent,
checks the durable intent and Stage bindings, and obtains the action from
`discover_live_apply_recovery()`; callers cannot choose an operation or recovery
mode.

Each invocation performs at most one of these actions:

| Durable decision | Controller behavior |
| --- | --- |
| `start_next` | Delegate exactly that operation to the journaled writer and return. |
| `reconcile_uncertain` | Delegate exactly that operation to the read-only reconciler and return. |
| `blocked` | Report the durable block and any exact `not_applied` or `ambiguous` outcome without invoking the writer. |
| `complete` | Return a no-op completion result. |

An `applied` reconciliation transitions the interrupted operation to
`mutation_verified`, but the controller does not begin the next operation in the
same call. A later invocation must rediscover it as `start_next`. Exact baseline
(`not_applied`) and conflicting or unsafe (`ambiguous`) observations remain
blocked. They never become automatic retries.

The existing StateStore lifetime lock excludes a second process. SQLite
transactions, immutable deployment/plan identities, and the writer's
journal-before-mutation order preserve idempotency within the owning process. The
controller adds no alternate filesystem primitive and persists no candidate bytes,
clear paths, credentials, exception text, or secrets. Delegated failures are
reported through bounded diagnostic categories.

This boundary does not reload or restart Home Assistant, accept an observation
window, promote or tag Git history, restore a backup, reject a candidate, or run
rollback. Those remain separate controlled-deployment gates required by the
initial V2 README. Retrigger may call this boundary only with the complete freshly
verified evidence chain and must continue to respect durable blocked outcomes and
bounded retry policy.
