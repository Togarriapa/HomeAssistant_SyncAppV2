# Bounded Recorder Retrigger pass

The initial V2 README requires missed Recorder snapshots and interrupted operations to survive restarts and be recoverable by Retrigger handling. `run_database_sync_retrigger_pass()` is one bounded recovery/execution pass for that work class; it is not the periodic cron dispatcher.

Each pass first applies the existing durable interruption recovery, then atomically claims at most one `database` work item. Work from Local -> Repo B synchronization, candidate deployment, runtime inventory, logs, and future work classes is not consumed by the database claim.

The caller must provide the current absolute Recorder database path, isolated database/snapshot/Git staging roots, the already configured Repo B target, and its authentication token. The pass does not discover a Recorder path or credentials. A claimed database item whose durable identity no longer matches the currently configured target/path is deterministically blocked instead of being executed or retried forever; a later pass may then process the current item.

Matching work executes the guarded database synchronization adapter. Successful publication/no-change completes the item, deterministic trusted-baseline conflicts block it, and transient guarded failures use the existing bounded retry/backoff state machine. Failures surfaced by the pass are sanitized.

This primitive does not schedule itself, restore a database, prune retention, process remote candidates, write Home Assistant configuration, or run Git inside the live Home Assistant tree.
