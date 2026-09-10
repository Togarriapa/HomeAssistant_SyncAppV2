# Normal Recorder startup bootstrap

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires consistent Recorder snapshots on the dedicated Repo B `database` branch while the Retrigger Work Cron Job remains a recovery mechanism. Normal database work therefore needs an execution source independent of Retrigger.

When Repo B has been verified and bound and `recorder_database_path` is explicitly configured, service startup schedules one routine database generation and invokes the existing bounded normal database processor. If the Recorder path is absent, no startup database work is created.

Before scheduling, the service resolves both the configured Recorder path and the read-only Home Assistant configuration root. The canonical Recorder source must remain strictly below the canonical `/homeassistant` boundary. Missing paths and parent-symlink escapes fail closed before database work is scheduled. Source-file type, uniqueness, stable identity and SQLite consistency are still revalidated by the existing snapshot pipeline when execution occurs.

Database staging, snapshot staging and Git workspace roots are app-owned private directories under `/data/syncapp/work`; they never overlap the live Home Assistant source. The service checks for shutdown after Local startup work and again after the database bootstrap before proceeding to Runtime startup.

This bootstrap uses the normal routine scheduler and normal processor only. It does not call interrupted-work recovery or administrative retry, and a deterministic blocked generation remains blocked.

This increment intentionally does not invent a periodic interval. Periodic scheduling remains a separate normal-work producer milestone. Candidate integrity/semantic validation, pre-deployment backup, Apply, reload/restart, observation, promotion, rollback and Home Assistant write permissions are unchanged.
