# Core and Supervisor log collection

The initial V2 README requires Repo B's generated `logs` branch to expose operational Home Assistant and Supervisor diagnostics without making logs a deployment source.

This collector uses Home Assistant's supported Supervisor REST interface. The App enables `hassio_api: true` with the explicit default Supervisor role while retaining AppArmor, protection-compatible settings, and the existing read-only Home Assistant configuration mount. It does not enable Docker API access, host namespaces, privileged mode, `full_access`, or manager/admin Supervisor roles.

## Bounded retrieval

`collect_supervisor_logs()` performs exactly two authenticated GET requests:

- `/core/logs?lines=2000&no_colors`
- `/supervisor/logs?lines=2000&no_colors`

The Supervisor token is carried only in the `Authorization: Bearer` header. URLs and error messages never contain it. Each request has a default ten-second timeout and four-MiB response limit. Only successful UTF-8 `text/plain` or `text/x-log` responses are accepted. NUL-containing payloads and individual records larger than one MiB fail closed.

The two sources remain distinct as the existing `home-assistant` and `supervisor` log-artifact categories. Each returned line receives a deterministic SHA-256 record identifier derived from its category, response position, and exact UTF-8 bytes. The collection reference time is used as the record timestamp because plain Supervisor log responses do not provide a stable documented structured timestamp schema that V2 can safely depend upon yet.

That timestamp choice means this bounded collector is a current diagnostic snapshot, not a claim that the retrieved 2,000 lines span the entire README-defined 30-day period. The existing artifact layer enforces 30-day retention for records supplied with trustworthy timestamps; future cursor/verbose-history work may extend source coverage without weakening this boundary.

## All-or-nothing artifact handoff

`collect_and_enqueue_supervisor_logs()` does not create an artifact until **both** required source requests succeed. It then uses the existing atomic `build_log_artifact()` primitive and idempotently enqueues that exact artifact ID into the durable `logs` work lane. Publication remains asynchronous and guarded by the existing logs synchronization/retrigger transaction.

A source failure therefore creates neither a mixed partial artifact nor durable publication work. Repeating an identical collection with the same reference time produces the same artifact/work identity.

This capability does not modify Home Assistant configuration, perform candidate validation, create a deployment backup, Apply candidate bytes, alter rollback behavior, or change the Retrigger Work Cron Job schedule. Git retention still does not provide forensic secure deletion; the README's history-retention requirement remains a separate concern.
