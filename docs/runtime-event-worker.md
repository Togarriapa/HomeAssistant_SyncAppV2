# Isolated runtime event worker

The initial V2 README requires runtime information to be collected automatically while keeping synchronization reliable, recoverable, observable, and event-driven where practical. The runtime event worker is an incremental transport boundary for that objective.

The worker owns its own thread and asyncio event loop. It may hold the Home Assistant Supervisor credential only for the lifetime of its transport calls, and it never receives or accesses `StateStore`. Cross-thread communication is restricted to the bounded `RuntimeEventMailbox`, which accepts only a readiness signal or an allow-listed normalized event type. Raw Home Assistant WebSocket event payloads are not retained by this boundary.

Transient `CoreEventStreamError` failures are retried with capped exponential backoff and a bounded attempt count. A stop request interrupts both an active consumer wait and reconnect backoff. Terminal worker results expose only reason class and bounded counters.

Mailbox rejection or saturation is a deterministic local safety failure, not a transport outage. The callbacks mark that condition before propagating it, so even when the Core stream sanitizes the callback exception into `CoreEventStreamError`, the worker terminates as failed instead of reconnecting and repeatedly filling the queue.

The worker is deliberately not started from the service entrypoint in this slice. Owner-thread mailbox draining and routine runtime scheduling must remain separate until their lifecycle integration is independently designed and tested. This capability does not alter Retrigger semantics and does not authorize candidate validation bypass, backup, Apply, reload/restart, observation, promotion, rollback, or any live Home Assistant configuration mutation.
