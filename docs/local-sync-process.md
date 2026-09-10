# Normal Local synchronization processor

The initial V2 README requires local Home Assistant configuration changes to be detected automatically, debounced until stable, staged outside the live configuration directory, verified, compared with the last synchronized state and published only when meaningful differences exist.

`run_local_sync_process()` is the bounded non-Recovery execution boundary for one already-scheduled Local → Repo B `main` generation. It claims at most one eligible `local_sync` work item, verifies that the durable work identity matches the configured repository target and branch, then delegates to the existing guarded Local synchronization workflow. A mismatched identity is blocked deterministically rather than redirected to another repository or branch.

This processor intentionally does not call `recover_interrupted_work()` and does not invoke the Retrigger recovery pass. Interrupted/transient recovery remains the responsibility of the separate Retrigger Work Cron Job described by the root README.

The processor also does not invent a local-change polling cadence. A separate producer must detect and debounce stable changes before calling the existing routine scheduler. Keeping detection separate prevents the service loop from turning into an unconditional full-tree snapshot poll.

No Git operation is performed inside the live Home Assistant configuration tree. This component does not participate in candidate deployment and does not authorize validation bypass, backup, Apply, reload/restart, observation, promotion or rollback.
