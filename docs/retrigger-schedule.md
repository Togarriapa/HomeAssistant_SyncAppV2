# Retrigger recovery schedule

Specification source: initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The Retrigger Work Cron Job is a recovery mechanism, not the normal producer for synchronization work. Normal Local, Recorder, runtime, logs and candidate detection paths keep their existing event/cadence behavior. Retrigger exists to recover interrupted, missed or transiently failed durable work.

## Scheduling contract

- The recovery scheduler is armed whenever trusted Repo B synchronization is active; there is no disable option.
- `retrigger_interval_seconds` is configurable from 30 through 3600 seconds and defaults to 300 seconds.
- The first automatic recovery cycle waits one full configured interval after service activation. Startup bootstrap remains responsible for normal immediate work.
- Elapsed intervals coalesce into at most one bounded recovery cycle per owner-loop tick; missed intervals never create a catch-up queue.
- The scheduler runs on the StateStore-owning service thread and delegates to the existing bounded Retrigger cycle, preserving exclusive state ownership and preventing concurrent recovery cycles.
- A cycle result does not bypass or reset durable work state. Transient backoff and attempt limits remain owned by the existing work state machine; blocked deterministic failures remain blocked until a changed work identity or explicit administrative retry makes them eligible.
- Shutdown disarms the schedule before StateStore ownership is released.

## Deployment safety

The schedule adds no candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging or rollback authority. Candidate recovery may only enqueue/advance work through the already-guarded durable workflow. The scheduler must never turn a deterministic candidate failure into an automatic retry.

This slice does not claim that a Linux `cron` daemon is required inside the Home Assistant App container. The README's named Retrigger Work Cron Job is implemented as the app-owned recurring recovery schedule so it can retain the single StateStore/process lock and supported Home Assistant App lifecycle rather than creating a second state-owning process.
