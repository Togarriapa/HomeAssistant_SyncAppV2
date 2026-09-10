# Local configuration change debounce

The sole product specification for this capability is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires automatic Local -> GitHub synchronization to detect a stable change and debounce rapidly changing files before the existing isolated snapshot/publication path is used. Retrigger remains a separate recovery mechanism.

`LocalChangeDebouncer` is the bounded owner-thread state machine for that quiet-period boundary. It accepts only normalized monotonic-time change notifications from a future trusted filesystem-event transport. Each notification replaces the pending deadline with `notification_time + quiet_seconds`, so repeated writes are coalesced until the source has remained quiet for the entire interval.

A tick before the deadline does nothing. The first tick at or after the deadline schedules at most one normal Local generation through `schedule_local_sync_generation()`, then clears the pending deadline. Further ticks do nothing until another change notification arrives. Existing routine scheduling semantics remain authoritative: active/retry work is coalesced, a prior success may become a fresh generation, and deterministic blocked work is not automatically rearmed.

The component rejects invalid or backward-moving monotonic time rather than risking premature synchronization. It stores no file paths, file contents, Git credentials, or Home Assistant configuration bytes.

This increment intentionally does not select or activate a filesystem event backend and does not invent a polling cadence. A later service-integration slice must provide a bounded local event source compatible with Home Assistant OS and feed only normalized change signals into this debouncer. Snapshot creation, integrity verification, Git publication and divergence handling remain in the existing Local synchronization pipeline.

Candidate validation, backup, Apply, reload/restart, observation, promotion and rollback are unchanged. The Retrigger Work Cron Job is not used as the normal scheduler by this component.
