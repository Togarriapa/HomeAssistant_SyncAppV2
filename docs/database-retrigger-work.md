# Durable Recorder Retrigger work

The initial V2 README requires missed database snapshots and interrupted work to be recoverable through the Retrigger Work Cron Job. This increment gives guarded Recorder publication a durable `database` work adapter without enabling the periodic dispatcher itself.

A work item is deterministically bound to the case-insensitive Repo B target and the caller-supplied absolute Recorder database path. The adapter never discovers or guesses a Recorder location. It claims only `database` work through the public atomic `StateStore.claim_work_kind()` boundary and executes only an already-claimed matching item.

Guarded database outcomes are persisted as follows:

- initialized, published, or no-change -> `succeeded`;
- baseline required, remote divergence, or a missing remote branch after a recorded baseline -> `blocked` with no automatic retry;
- guarded synchronization failure -> the existing bounded retry/backoff state machine.

The underlying publication path still creates a consistent SQLite online backup, stages it outside the Home Assistant tree, uses isolated Git workspaces, rejects unsafe Repo B state, and publishes only with non-force verified transport. No database bytes are deployed from GitHub into Home Assistant by this adapter.

This is a recovery primitive for the future Retrigger dispatcher. It does not schedule work, infer custom Recorder paths, prune database retention, restore Recorder data, or process the `candidate` branch.
