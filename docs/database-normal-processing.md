# Normal Recorder Database Processing

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires Recorder database snapshots to be published periodically while the Retrigger Work Cron Job remains a recovery mechanism. Those responsibilities are intentionally separate.

`database_sync_process.run_database_sync_process()` is the bounded execution boundary for ordinary database work. It claims at most one eligible durable `database` item, verifies that the item identity matches both the configured Repo B target and the exact absolute Recorder database path, then delegates to the existing guarded Recorder snapshot publication pipeline.

A work-key mismatch is treated as deterministic and blocked before any publication attempt. Transient failures remain subject to the existing bounded retry policy. The processor never calls interrupted-work recovery and never performs administrative retry.

`database_sync_retrigger.run_database_sync_retrigger_pass()` remains the recovery adapter. It first invokes interrupted-work recovery, then delegates one bounded processing attempt to the same normal processor. This preserves one publication implementation while preventing Retrigger from becoming the ordinary scheduler.

The bounded [routine Recorder snapshot service](routine-recorder-service.md) now
invokes this processor after its monotonic periodic scheduler becomes due. Retrigger
remains independently responsible for recovery.

All database copies, snapshots and Git metadata remain in the already-established protected staging/workspace paths. No Git operation is performed in the live Home Assistant configuration tree, and this processing boundary does not implement Candidate deployment, backup, Apply, reload/restart, observation, promotion or rollback.
