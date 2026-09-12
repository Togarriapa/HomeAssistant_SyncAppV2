# Routine Work Scheduling vs Retrigger Recovery

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README separates **normal synchronization**, which should be event-driven wherever practical, from the **Retrigger Work Cron Job**, which exists to recover interrupted or transiently failed work. These are different lifecycle responsibilities and must remain different in code.

`work_schedule.schedule_routine_work()` is the durable boundary for a normal producer to announce a new generation of work. A missing identity becomes `pending`. A previously `succeeded` identity is rearmed as a fresh `pending` generation with attempts reset to zero and fresh scheduling timestamps. Repeated routine signals are coalesced while work is already `pending`, `running`, or `retry`.

A `blocked` item is never rearmed by routine scheduling. Deterministic failures remain blocked exactly as required by the README. Only the separately authorized administrative retry boundary may make blocked work eligible again.

The lane adapters preserve the existing deterministic identities:

- `local_sync_schedule.schedule_local_sync_generation()` identifies one Repo B target and branch.
- `database_sync_schedule.schedule_database_sync_generation()` identifies one Repo B target and absolute Recorder database path.
- `runtime_sync_schedule.schedule_runtime_sync_generation()` identifies the Repo B runtime publication lane for one target.

These adapters schedule work only. They do not execute synchronization and they do
not change Retrigger passes. The Local event bridge, Runtime event bridge, and
[routine Recorder service](routine-recorder-service.md) provide bounded normal
producers while preserving that separation.

## Normal runtime processing

`runtime_sync_process.run_runtime_sync_process()` is the bounded execution boundary for normal runtime work. It claims and processes at most one eligible `runtime` item through the existing Core runtime collection and isolated publication pipeline. It does **not** recover interrupted work and does not rearm deterministic blocked work.

A Core runtime collection failure moves only the claimed item through the existing transient retry policy. A target mismatch blocks only that claimed item. Unexpected state or runtime-work failures fail closed with sanitized diagnostics.

`runtime_sync_retrigger.run_runtime_sync_retrigger_pass()` remains the recovery adapter: it first performs interrupted-work recovery and only then delegates one processing attempt to `run_runtime_sync_process()`. This keeps normal event-driven execution reusable without silently importing Retrigger semantics into the service path.

## Service-start runtime bootstrap

After the service has re-verified and pinned the configured private Repo B identity, `runtime_startup.run_startup_runtime_sync()` schedules one normal runtime generation and immediately processes at most one eligible runtime item. This gives a newly started service a deterministic opportunity to publish a fresh AI-readable runtime baseline without waiting for the Retrigger recovery job.

The startup bootstrap uses only app-owned private staging, snapshot and Git-workspace roots beneath `/data/syncapp/work`. It never puts Git metadata or generated runtime files in the live Home Assistant configuration tree. A transient Core collection failure remains durable as `retry`; a deterministic blocked item remains blocked. Startup never calls interrupted-work recovery or administrative retry.

If shutdown has already been requested before bootstrap begins, the service skips this work. An unconfigured service also remains passive and performs no runtime publication attempt.

This startup bootstrap is deliberately distinct from the long-running WebSocket lifecycle. It establishes a fresh baseline opportunity at process start but does not yet attach asynchronous event consumption to the synchronous state-owning service loop.

## Runtime event classification

`runtime_event_trigger.schedule_runtime_for_event()` is a narrow boundary between Home Assistant event transport and durable runtime scheduling. It accepts only a normalized mapping containing one bounded `event_type` string. It does not retain raw Home Assistant event payloads.

Events that can change the runtime view—state, entity/device/area/floor/label/category registries, loaded components, Core configuration, and service registration—schedule the existing runtime generation. Unrelated events are ignored. Malformed evidence fails closed. Because the function delegates to routine scheduling, repeated events are coalesced while work is active and a deterministic blocked failure stays blocked.

### Owner-thread mailbox

`runtime_event_mailbox.RuntimeEventMailbox` is the bounded bridge intended for future cross-context event transport. It accepts only two immutable signal shapes: verified subscription `ready`, or one allowed normalized `event_type`. Raw Home Assistant event payloads, credentials and arbitrary objects are not part of the mailbox schema.

The mailbox is capacity-bounded and uses non-blocking insertion. Capacity exhaustion fails closed rather than silently dropping evidence. A future transport owner can therefore disconnect/retry and rely on the next verified readiness baseline to close any observation gap instead of pretending an overflowing event stream was complete.

`drain_runtime_event_mailbox()` must run on the `StateStore`-owning thread. It consumes at most a fixed bounded number of signals per call, revalidates every signal, and delegates only to the existing normal runtime scheduling boundaries. It performs no interrupted-work recovery. Repeated ready/events coalesce into the same durable runtime work identity, and deterministic blocked work remains blocked.

This mailbox does not itself start a thread, process, socket or WebSocket lifecycle. Those ownership decisions remain a separate service-integration gate.

## Core WebSocket transport

`core_event_stream.consume_core_runtime_events()` implements the read-only transport boundary against the documented Home Assistant App proxy at `ws://supervisor/core/websocket`. It performs the documented authentication handshake using `SUPERVISOR_TOKEN`, subscribes only to the classifier's deterministic event-type set, verifies every subscription result, enforces message-size and connection timeout limits, and rejects binary, malformed, unknown-ID, or mismatched event evidence.

The transport never forwards raw event data. A valid incoming event is reduced to `{"event_type": "..."}` before the injected handler is invoked. The token is used only in the authentication frame and is neither persisted nor included in diagnostics.

### Verified readiness and baseline refresh

Events may arrive while the individual WebSocket subscriptions are still being confirmed. Those setup-time events are intentionally validated but not forwarded because the complete event-observation boundary is not established yet.

After every requested subscription has returned a verified successful result, the transport invokes its optional `on_ready` callback exactly once before forwarding ordinary events. `RuntimeEventSession.ready()` uses that point to schedule one complete runtime inventory generation. The resulting runtime snapshot therefore observes current state after the full subscription set is active, closing the setup window without trusting partial event data.

`RuntimeEventSession.event()` then delegates each normalized event type to the existing classifier. Both callbacks can only schedule durable routine work; they cannot execute synchronization directly. A deterministic blocked runtime failure remains blocked even across reconnection readiness signals.

### Bounded reconnect lifecycle

`runtime_event_lifecycle.run_runtime_event_lifecycle()` wraps the verified subscriber in a bounded reconnect transaction. Each connection attempt gets a fresh `RuntimeEventSession`. A connection that reaches verified subscription readiness schedules a complete runtime baseline before ordinary normalized event scheduling resumes.

Only `CoreEventStreamError` failures are eligible for automatic reconnect inside one lifecycle invocation. Reconnect attempts use deterministic exponential backoff with a configured cap and a hard maximum attempt count, so a broken or incompatible stream cannot create a tight or infinite retry loop. Unexpected internal failures fail closed immediately with sanitized diagnostics.

Backoff waits are interruptible through an `asyncio.Event`. Shutdown therefore prevents a further connection attempt and does not schedule additional routine work. Exhausting the bounded attempt budget fails closed; a later service/recovery decision may start a new bounded lifecycle, but this primitive does not silently convert permanent failure into an infinite loop.

The token remains an ephemeral argument to the subscriber and is not persisted or returned in lifecycle results. Raw Home Assistant event payloads still terminate at the transport normalization boundary.

The long-running reconnect lifecycle remains intentionally **unwired from `__main__.py`** in this increment. Service activation must independently solve safe ownership/concurrency between asynchronous event transport and the process-exclusive synchronous `StateStore` before attaching that task to the application lifecycle.

This work intentionally does **not** make Retrigger a periodic normal scheduler. It also does not implement Candidate deployment, semantic Home Assistant validation, backup, Apply, reload/restart, observation, promotion, rollback, or writes to the live Home Assistant configuration tree.
