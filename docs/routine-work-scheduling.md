# Routine Work Scheduling vs Retrigger Recovery

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README separates **normal synchronization**, which should be event-driven wherever practical, from the **Retrigger Work Cron Job**, which exists to recover interrupted or transiently failed work. These are different lifecycle responsibilities and must remain different in code.

`work_schedule.schedule_routine_work()` is the durable boundary for a normal producer to announce a new generation of work. A missing identity becomes `pending`. A previously `succeeded` identity is rearmed as a fresh `pending` generation with attempts reset to zero and fresh scheduling timestamps. Repeated routine signals are coalesced while work is already `pending`, `running`, or `retry`.

A `blocked` item is never rearmed by routine scheduling. Deterministic failures remain blocked exactly as required by the README. Only the separately authorized administrative retry boundary may make blocked work eligible again.

`local_sync_schedule.schedule_local_sync_generation()` applies this rule to the existing deterministic Local -> Repo B work identity. It schedules work only; it does not execute synchronization and it does not change the Retrigger pass. A later event source may use this adapter after it has independently established that a stable local-change event should produce routine Local -> Repo B work.

This slice intentionally does **not** make Retrigger a periodic normal scheduler. It also does not implement Candidate deployment, semantic Home Assistant validation, backup, Apply, reload/restart, observation, promotion, rollback, or writes to the live Home Assistant configuration tree.
