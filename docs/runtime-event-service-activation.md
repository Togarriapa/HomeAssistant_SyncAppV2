# Configured runtime event service lifecycle

This increment activates the V2 runtime event transport only for a configured Repo B whose private repository identity has already been verified and durably bound.

Startup ordering is deliberate:

1. load validated app options;
2. verify and bind the configured private Repo B identity;
3. start the durable service run;
4. run the existing bounded startup runtime synchronization;
5. if shutdown has not been requested, construct and start the isolated runtime event bridge;
6. enter the normal service loop.

Unconfigured instances retain passive behavior and do not start the event transport.

During active operation the service owner thread continues to own `StateStore`. Each loop iteration serves at most one Retrigger IPC request and, independently, performs one runtime event bridge tick. The bridge drains only bounded normalized event signals and invokes the existing normal runtime work processor for at most one eligible item. It never calls the Retrigger cycle.

Graceful SIGTERM/SIGINT leaves the loop, stops the event worker cooperatively with its bounded join, then calls `StateStore.finish_run()`. If the runtime event bridge fails unexpectedly, the service attempts sanitized transport cleanup but deliberately does **not** mark the durable run finished; the next V2 start can therefore identify an interrupted run. The top-level service maps a bridge failure to the sanitized `runtime_events_unavailable` reason and does not print transport exception text or credentials.

This activation does not alter candidate deployment. It does not authorize candidate backup, Apply, reload/restart, observation, promotion, rollback, or any live Home Assistant configuration mutation. The root README's isolated candidate validation transaction remains a separate prerequisite.
