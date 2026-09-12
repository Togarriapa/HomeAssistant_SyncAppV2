# Routine Core and Supervisor log service

The sole product specification for this capability is the initial V2 root
`README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.
It requires diagnostic log publication on Repo B's dedicated `logs` branch while
keeping normal production separate from Retrigger recovery.

When private Repo B authentication and its pinned repository identity are
available, the StateStore-owning process builds and arms `LogSyncService`. An
unconfigured service remains passive. The service uses the existing bounded,
all-or-nothing Supervisor collector: both the Core and Supervisor snapshots must
succeed before an immutable artifact and durable `logs` work identity are created.

The initial cadence is one hour. This interval is independent of both status-log
timing and the external Retrigger schedule. The first deadline is armed from
`time.monotonic()` after normal startup completes; merely restarting the App does
not immediately create a duplicate log snapshot. A delayed owner loop advances
the next deadline from the current observation, coalescing all elapsed intervals
into one collection rather than replaying a catch-up queue. Invalid, non-finite,
or backward-moving clock evidence fails closed.

Each due tick performs one complete collection and calls the bounded normal logs
processor once. The processor may claim at most one eligible durable log item,
which can be an older retryable item rather than the artifact just collected.
Exact artifact/work identity prevents duplicate publication. App-owned roots for
artifacts, snapshots, and Git workspaces are created and verified without following
symlinks, and the existing loader, integrity checks, isolated Git transaction,
non-force publication, remote verification, and durable baseline are reused.
Publication authority remains limited to `logs`.

A failed source request creates no partial artifact or work. A transient failure
after durable enqueue leaves pending or bounded-retry evidence for the independent
Retrigger pass. Deterministic malformed, foreign, missing, or tampered work remains
blocked. The normal service neither recovers interrupted jobs nor provides an
administrative retry path, so it cannot become a parallel recovery engine.

Shutdown is checked before each owner-loop tick. The service is disarmed before
StateStore ownership is released. Unexpected collection or processing failure is
sanitized, stops all active service components, and deliberately leaves the run
marked interrupted for recovery diagnostics.

This service reads diagnostics only. It cannot write the live Home Assistant
configuration, publish Repo B `main` or `candidate`, or perform Candidate Apply,
backup, reload/restart, observation, promotion, tagging, or rollback. It does not
modify or invoke the external Retrigger Work Cron Job as a normal producer.

## Verification evidence

Issue #217 is covered by primitive and service-level tests for initial deadlines,
hourly cadence, collect-before-process ordering, missed-interval coalescing,
invalid/backward clock rejection, all-or-nothing collection failure, private-root
verification, disabled configuration, trusted startup ordering, shutdown before
activation, clean disarming, sanitized failure, and interrupted-run preservation.

The exact pull-request head must pass formatting, Ruff, mypy, Bandit, full pytest,
and native amd64/aarch64 container lifecycle and semantic validation before merge.
Physical Home Assistant OS installation remains separate platform evidence.
