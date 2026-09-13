# Retrigger recovery schedule

Specification source: initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The Retrigger Work Cron Job is a recovery mechanism, not the normal producer for synchronization work. Normal Local, Recorder, runtime, logs and candidate detection paths keep their existing event/cadence behavior. Retrigger exists to recover interrupted, missed or transiently failed durable work.

## Scheduling contract

- The recovery dispatcher is active whenever Repo B is configured; the state-owning service still re-proves private Repo B identity before every recovery cycle. There is no disable option.
- `retrigger_interval_seconds` is configurable from 30 through 3600 seconds and defaults to 300 seconds.
- The first automatic recovery request waits one full configured interval after App activation. Startup bootstrap remains responsible for normal immediate work.
- Elapsed intervals coalesce into at most one bounded request; missed intervals never create a catch-up queue.
- The launcher owns only cadence. It never opens the StateStore. Every due recovery request crosses the existing private same-owner Unix socket to the sole StateStore-owning service process, which executes the bounded Retrigger cycle synchronously. This preserves exclusive state ownership and prevents overlapping recovery cycles.
- IPC dispatch failure advances the next deadline rather than creating a tight retry loop. Once a request reaches the service, durable work retry/backoff and blocked-state semantics remain authoritative.
- Recorder configuration is optional. Without an explicit Recorder source, the database lane is skipped while Local, runtime, logs and candidate recovery continue.
- A cycle never invokes administrative retry. Blocked deterministic failures remain blocked until a changed work identity or explicit administrative retry makes them eligible.
- Container shutdown is forwarded to the service and disarms the cadence as the service exits.
- Home Assistant App `boot: auto` returns the recovery mechanism after a host reboot.

## Deployment safety

The schedule adds no candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging or rollback authority. Candidate recovery may only proceed through the already-guarded durable workflow. The scheduler must never turn a deterministic candidate failure into an automatic retry.

The initial README names this mechanism the Retrigger Work Cron Job. Inside the Home Assistant App it is implemented as a dedicated recurring dispatcher supervised beside the state-owning service, rather than a second state-mutating `cron` process. This preserves the supported App lifecycle and the single durable-state/process-lock boundary while retaining a distinct recovery cadence.
