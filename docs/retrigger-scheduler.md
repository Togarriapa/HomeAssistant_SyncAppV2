# Recurring Retrigger owner scheduler

The Retrigger Work Cron Job is implemented as a recurring monotonic scheduler inside the single SyncApp process that already owns `StateStore`. It is not a second process and it never opens durable state concurrently with the service.

## Cadence and activation

`retrigger_interval_seconds` is mandatory in the effective runtime configuration, defaults to 300 seconds, and is bounded from 30 through 3600 seconds. There is no disable sentinel. The scheduler is created only after the configured Repo B has been authenticated, verified private, and bound to its durable repository ID.

The first automatic cycle becomes due one full interval after arming. If the owner loop is delayed across multiple intervals, the missed deadlines are coalesced: one cycle is executed and the next deadline is measured from that owner-loop tick. No backlog or catch-up queue is created and cycles cannot overlap because scheduling and execution remain on the StateStore owner thread.

Monotonic time is validated and a backward clock observation fails closed rather than making retry cadence ambiguous. Shutdown disarms the scheduler before the StateStore context is released, including exception cleanup paths after service activation.

## Recovery boundary

Each due tick delegates to the existing bounded `run_retrigger_cycle()` path after re-verifying the same private Repo B identity. The scheduler does not call administrative retry or alter durable operation state directly. Existing unique work identifiers, atomic claims, stale/interrupted recovery, retry/backoff eligibility, deterministic blocking, and rejected candidate semantics remain authoritative.

Local configuration, runtime inventory, logs, and candidate detection/recovery remain available when Recorder is not configured. In that case only the `database` recovery lane returns a no-work result; configuring `recorder_database_path` enables the existing guarded Recorder lane.

Automatic completion emits only the fixed event `retrigger_cycle_completed`, mode `automatic`, and a sanitized fixed outcome such as `completed`, `repo_b_untrusted`, `cycle_failed`, or `internal_error`. Credentials, source paths, repository contents, and nested exception text are not emitted.

## Safety invariants

Scheduling does not add candidate Fetch/Stage/Apply authority and does not bypass validation, backup, reload/restart, observation, promotion, rollback, deterministic commit blocking, or any existing publication lease. It adds no Home Assistant write mount. The production container enters `ha_syncapp.service_entry`, which preserves the existing CLI/error contract while replacing only the owner service run implementation with the recurring scheduler-aware loop.

The Retrigger schedule is part of normal service lifecycle and must never be paused, disabled, or deleted as an operational workaround. Deterministic failures remain blocked until their underlying work identity changes or an explicit administrative retry is requested through the existing guarded administrative mechanism.
