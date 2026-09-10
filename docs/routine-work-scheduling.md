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

## Runtime event classification

`runtime_event_trigger.schedule_runtime_for_event()` is a narrow boundary between Home Assistant event transport and durable runtime scheduling. It accepts only a normalized mapping containing one bounded `event_type` string. It does not retain raw Home Assistant event payloads.

Events that can change the runtime view—state, entity/device/area/floor/label/category registries, loaded components, Core configuration, and service registration—schedule the existing runtime generation. Unrelated events are ignored. Malformed evidence fails closed. Because the function delegates to routine scheduling, repeated events are coalesced while work is active and a deterministic blocked failure stays blocked.

## Core WebSocket transport

`core_event_stream.consume_core_runtime_events()` implements the read-only transport boundary against the documented Home Assistant App proxy at `ws://supervisor/core/websocket`. It performs the documented authentication handshake using `SUPERVISOR_TOKEN`, subscribes only to the classifier's deterministic event-type set, verifies every subscription result, enforces message-size and connection timeout limits, and rejects binary, malformed, unknown-ID, or mismatched event evidence.

The transport never forwards raw event data. A valid incoming event is reduced to `{"event_type": "..."}` before the injected handler is invoked. The token is used only in the authentication frame and is neither persisted nor included in diagnostics.

The subscriber remains intentionally **unwired from the service lifecycle**. Reconnection/backoff, shutdown behavior, and the exact service-level handler are a separate milestone that must preserve bounded work and must not turn transient event-stream failures into unsafe Home Assistant mutations.

This work intentionally does **not** make Retrigger a periodic normal scheduler. It also does not implement Candidate deployment, semantic Home Assistant validation, backup, Apply, reload/restart, observation, promotion, rollback, or writes to the live Home Assistant configuration tree.
