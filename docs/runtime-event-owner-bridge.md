# Owner-thread runtime event bridge

The initial V2 README requires Home Assistant runtime information to remain useful to AI analysis while normal synchronization is event-driven where practical and durable state stays recoverable. This bridge connects the isolated runtime event transport to the existing normal runtime work processor without changing those ownership boundaries.

`RuntimeEventWorker` continues to own only its transport thread and asyncio loop. It never receives `StateStore`. The worker emits only normalized readiness/event signals into the bounded `RuntimeEventMailbox`. A non-blocking worker status method exposes only terminal reason and bounded counters.

`RuntimeEventBridge` is created on the `StateStore` owner thread and rejects lifecycle calls from other threads. Each `tick()` first verifies that the transport has not terminated unexpectedly, drains a bounded batch of mailbox signals through the existing routine scheduler, then invokes the existing normal runtime processor for at most one eligible durable runtime item. The processor is invoked even on an empty mailbox so an already-eligible normal retry is not dependent on receiving another Home Assistant event.

Routine scheduling retains its existing semantics: pending/running/retry work is coalesced, successful work may become a fresh generation, and blocked deterministic work is never automatically rearmed. The separate Retrigger recovery path is not called or modified by this bridge.

Shutdown requests cooperative worker cancellation and performs a bounded join. Unexpected transport completion, retry exhaustion, deterministic worker failure, owner-thread misuse, mailbox failure, processor failure, or invalid lifecycle transitions fail closed with sanitized bridge errors.

This increment deliberately does **not** activate the bridge from `__main__.py`. Service activation is a separate reviewable step so startup ordering, graceful shutdown, logging, and failure policy can be tested without weakening the transport/StateStore separation.

Nothing in this bridge authorizes candidate backup, Apply, reload/restart, observation, promotion, rollback, or live Home Assistant configuration mutation. Candidate deployment remains downstream of the root README's validation transaction and the unresolved version-matched semantic validation gate.
