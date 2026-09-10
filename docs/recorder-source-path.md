# Recorder source path selection

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires consistent Home Assistant Recorder snapshots to be isolated on the dedicated Repo B `database` branch. Selecting the source database is a separate trust boundary from taking or publishing a snapshot: SyncApp must not guess which database file represents Recorder state.

`recorder_database_path` is therefore optional and has no implicit default. When it is absent, normal Recorder work remains unscheduled. When configured, it must be a normalized absolute path strictly beneath the supported read-only `/homeassistant` Home Assistant configuration mount. The mount root itself, relative paths, traversal, repeated separators, trailing separators, control characters, and paths outside `/homeassistant` are rejected during option parsing before any database work can be scheduled.

This option only selects a potential source path. It does not establish that the target exists, is a regular SQLite Recorder database, or is safe to snapshot. Those properties remain the responsibility of the existing fail-closed database snapshot/publication pipeline, which revalidates source identity and SQLite consistency when work executes.

No path is inferred from Home Assistant conventions. In particular, SyncApp does not silently assume `/homeassistant/home-assistant_v2.db`; custom Recorder locations must be provided explicitly until a separately validated discovery mechanism exists.

This increment does not choose a periodic cadence, invoke Retrigger recovery, perform administrative retry, or change Candidate validation, pre-deployment backup, Apply, reload/restart, observation, promotion, rollback, or Home Assistant write access.
