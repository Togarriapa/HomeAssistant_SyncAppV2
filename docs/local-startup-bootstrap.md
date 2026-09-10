# Normal Local Synchronization Startup Bootstrap

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README separates normal Local → GitHub synchronization from the Retrigger Work Cron Job. Normal synchronization should be event-driven wherever practical; Retrigger exists to recover interrupted or transiently failed work. Service startup therefore provides one bounded normal Local synchronization opportunity without invoking recovery semantics.

After the configured private Repo B has been re-verified and its stable repository ID bound in protected state, `local_startup.run_startup_local_sync()` schedules one normal `local_sync` generation for Repo B `main` and processes at most one eligible Local item. It delegates to the existing guarded Local synchronization pipeline rather than implementing a second publication path.

The service reads Home Assistant configuration from the read-only `/homeassistant` App mount. Snapshot and Git workspace data remain under app-owned protected storage at `/data/syncapp/work/main-snapshots` and `/data/syncapp/work/main-workspaces`. Those protected work roots are verified not to overlap the live Home Assistant source tree before synchronization begins.

The guarded Local pipeline retains its existing safety properties: stable source snapshotting, copied-file integrity verification, isolated Git operations, divergence detection, non-force publication, remote-result verification and durable baseline completion. Startup does not perform Git operations in the live Home Assistant configuration directory.

Startup Local synchronization does not perform interrupted-work recovery, administrative retry, Candidate deployment, Home Assistant backup, Apply, reload/restart, observation, promotion or rollback. A deterministic blocked Local item remains blocked. Transient synchronization failures remain durable and retryable according to the existing bounded work policy, where the separate Retrigger job may later recover them.

The service checks the shutdown flag before beginning the Local bootstrap and again before proceeding to later startup work. If shutdown is already requested, no new Local generation is started. If shutdown is requested after the Local bootstrap, subsequent runtime bootstrap/event activation is skipped.

An unconfigured service remains passive: without both a Repo B target and GitHub credential, no Local startup work is scheduled or processed.
