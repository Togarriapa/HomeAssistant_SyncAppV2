# Routine Work Scheduling vs Retrigger Recovery

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README separates **normal synchronization**, which should be event-driven wherever practical, from the **Retrigger Work Cron Job**, which exists to recover interrupted or transiently failed work. These are different lifecycle responsibilities and must remain different in code.

`work_schedule.schedule_routine_work()` is the durable boundary for a normal producer to announce a new generation of work. A missing identity becomes `pending`. A previously `succeeded` identity is rearmed as a fresh `pending` generation with attempts reset to zero and fresh scheduling timestamps. Repeated routine signals are coalesced while work is already `pending`, `running`, or `retry`.

A `blocked` item is never rearmed by routine scheduling. Deterministic failures remain blocked exactly as required by the README. Only the separately authorized administrative retry boundary may make blocked work eligible again.

The lane adapters preserve the existing deterministic identities:

- `local_sync_schedule.schedule_local_sync_generation()` identifies one Repo B target and branch.
- `database_sync_schedule.schedule_database_sync_generation()` identifies one Repo B target and absolute Recorder database path.
- `runtime_sync_schedule.schedule_runtime_sync_generation()` identifies the Repo B runtime publication lane for one target.

These adapters schedule work only. They do not execute synchronization and they do not change Retrigger passes. Future normal producers may invoke them after independently establishing the appropriate event or periodic trigger required by the initial README.

This slice intentionally does **not** make Retrigger a periodic normal scheduler. It also does not implement Candidate deployment, semantic Home Assistant validation, backup, Apply, reload/restart, observation, promotion, rollback, or writes to the live Home Assistant configuration tree.
