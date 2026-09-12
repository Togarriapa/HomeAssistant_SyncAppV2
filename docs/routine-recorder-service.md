# Routine Recorder snapshot service

The sole product specification for this capability is the initial V2 root
`README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.
It requires consistent periodic Recorder snapshots on Repo B's `database` branch
while keeping normal production distinct from Retrigger recovery.

When private Repo B authentication, its pinned repository identity, and an
explicit `recorder_database_path` are available, startup first runs the existing
normal Recorder bootstrap. Only after that succeeds does the StateStore-owning
service build and arm `DatabaseSyncService`. An unconfigured Recorder path leaves
this service disabled.

The initial cadence is one hour. This conservative internal interval is independent
of status-log timing and the external Retrigger interval. The first deadline is
armed from `time.monotonic()` after startup; the startup bootstrap already provided
the initial generation, so no duplicate is scheduled immediately. A delayed owner
loop advances the next deadline from the current observation and schedules only one
generation instead of replaying every missed interval. Invalid, non-finite, or
backward-moving clock evidence fails closed.

Each due tick calls the existing routine database scheduler and then the bounded
normal database processor at most once. Their deterministic work key combines Repo
B and the exact absolute Recorder path. Repeated signals therefore coalesce while
work is pending, running, or retryable. A completed identity is rearmed for the new
generation; blocked deterministic work stays blocked until explicit administrative
retry.

The configured Recorder source is canonically proven to remain strictly beneath
the read-only Home Assistant root before the service is built. Existing app-owned
database staging, consistent SQLite snapshot, byte-integrity, isolated Git
workspace, guarded non-force publication, remote verification, and durable
baseline behavior are reused without another implementation. Publication remains
limited to the dedicated `database` branch.

Transient snapshot or transport failure is recorded as durable retry work with the
existing bounded backoff. The normal service never calls interrupted-work recovery
or administrative retry; Retrigger remains responsible for missed, interrupted, or
eligible retry work. A deterministic blocked result is not automatically rearmed.

Shutdown is checked before every service-loop tick. The periodic service is then
disarmed before the process releases protected StateStore ownership, preventing
later scheduling or processing. Unexpected periodic-service failure is sanitized,
stops active service components, and leaves the run marked interrupted for recovery
diagnostics.

This integration does not write Home Assistant configuration and grants no Repo B
`main` or `candidate` authority, Candidate Apply, backup, reload/restart,
observation, promotion, tagging, or rollback behavior. It does not modify or invoke
the external Retrigger Work Cron Job as a normal producer.

## Verification evidence

Issue #215 is covered by primitive and service-level tests for initial deadlines,
hourly cadence, schedule-before-process ordering, missed-interval coalescing,
backward-clock rejection, transient retry handoff, source containment and private
roots, disabled configuration, startup ordering, shutdown-before-activation,
clean disarming, sanitized failure, and interrupted-run preservation. Existing
database pipeline tests continue to prove consistent snapshots and publication only
to `database`.

The exact pull-request head must pass formatting, Ruff, mypy, Bandit, full pytest,
and native amd64/aarch64 container lifecycle and semantic validation before merge.
Physical Home Assistant OS installation remains separate platform evidence.
