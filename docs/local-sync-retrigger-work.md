# Durable Local-sync work adapter

The initial V2 README requires interrupted and retryable work to survive restarts
and be processed idempotently by Retrigger handling. This increment connects the
guarded Local → Repo B synchronization cycle to the existing durable work-state
machine without implementing the scheduler itself.

A Local-sync work item uses the work kind `local_sync` and a deterministic SHA-256
key derived from the case-insensitive repository target plus the case-sensitive
branch. Enqueuing the same target/branch is therefore idempotent.

The Local-sync queue has an atomic kind-scoped claim operation. It selects only
eligible `local_sync` pending/retry items and leaves candidate, database, runtime,
logs and any future work classes untouched even when those items are older. The
claim uses the same optimistic status/attempt evidence as the durable state
machine: transition to `running` and attempt increment happen in the same SQLite
transaction.

The executor accepts **only an already-claimed** `local_sync` item whose work key
matches the supplied target and branch. It does not call the global queue claim
operation, so adding other Retrigger job classes cannot cause a Local-sync worker
to consume work with different safety semantics.

Guarded synchronization outcomes are persisted as follows:

- `initialized`, `published`, and `no_change` → `succeeded`;
- `baseline_required`, `diverged`, and `remote_missing` → `blocked` with no
  automatic retry;
- exceptional Local-sync failures → the existing bounded retry/backoff state
  machine, which eventually blocks after the maximum attempt count.

A process interruption while an item is `running` continues to use the existing
`recover_interrupted_work()` mechanism. Lock-file deletion is never part of
recovery.

This adapter does not grant write access to `/homeassistant`, perform Git
operations there, implement the periodic Retrigger Work Cron Job dispatcher,
process remote `candidate` changes, create Home Assistant backups, apply
candidates, restart Home Assistant, observe deployments or roll back. Those
remain separate guarded increments derived from the initial V2 README.
