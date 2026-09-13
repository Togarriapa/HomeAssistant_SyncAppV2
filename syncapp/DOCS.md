# Foundation service (0.1.0)

This experimental version establishes lifecycle, protected state, Repo B trust and
safe synchronization/recovery primitives. Normal synchronization producers and the
separate Retrigger recovery dispatcher remain distinct: routine work is event/cadence
driven, while Retrigger periodically recovers interrupted, missed or transiently
failed durable work.

## Home Assistant configuration source

The app receives the supported Home Assistant App `homeassistant_config` mount at
`/homeassistant` **read-only**. This directory is the live Home Assistant
configuration source. It must never be initialized as a Git working tree and Git
commands must never run inside it.

The read-only mount is intentionally sufficient only for observing and staging
local configuration. Candidate deployment remains guarded by the initial V2
README's validation, pre-deployment backup, controlled apply, reload/restart,
observation, verification and automatic rollback requirements. Merely changing a
Git branch must never write directly into the running Home Assistant configuration.

The Supervisor-provided `/data` volume stores SyncApp's protected state and
credentials. Protection mode should remain enabled. The manifest uses cold backups
so Supervisor stops this service while backing up its persistent state.

## Test installation

Install Home Assistant SyncApp V2 from this repository on a **test Home Assistant
OS installation**. The app supports aarch64 (including Raspberry Pi 5) and amd64.
It is configured for automatic boot so recovery returns after a host reboot. Inspect
the Log tab after installation and restart. Before treating the configuration mount
as verified on hardware, confirm that `/homeassistant` is visible from the app and
that writes from the app are denied.

## Options

| Option | Default | Accepted values |
| --- | --- | --- |
| `log_level` | `info` | `info`, `warning`, `error` |
| `status_interval_seconds` | `300` | Integer from 30 through 3600 |
| `retrigger_interval_seconds` | `300` | Integer from 30 through 3600 |
| `repo_b` | unset | GitHub `owner/repository` target |
| `github_token` | unset | Credential used only for trusted Repo B operations |
| `recorder_database_path` | unset | Normalized absolute Recorder path below `/homeassistant` |
| `recorder_retention_days` | `7` | Integer from 1 through 365 |

`status_interval_seconds` controls status logging. `retrigger_interval_seconds`
controls the separate recovery dispatcher and has no disable sentinel. If Repo B
is not configured there is no remote durable work to recover; after trusted Repo B
configuration becomes active, the dispatcher runs automatically until service
shutdown. Unsupported keys, duplicate JSON keys and invalid values prevent startup.
Recorder synchronization remains optional; omitting `recorder_database_path` skips
only the Recorder recovery lane and does not disable Local, runtime, log or candidate
recovery.

## Retrigger recovery

The container launcher supervises the state-owning SyncApp service and triggers the
existing bounded Retrigger cycle through the protected same-owner Unix socket. The
scheduler never opens the StateStore and never owns durable work directly. That
preserves the service's exclusive process/state lock while retaining a distinct
recovery mechanism.

The first automatic recovery request occurs one full configured interval after the
app starts. Missed intervals coalesce into one request; no catch-up queue is built.
A failed IPC dispatch advances the next deadline instead of creating a tight retry
loop. Once a request reaches the service, existing durable work identities,
claiming, controlled transient backoff and deterministic blocked states remain
authoritative. The schedule never invokes administrative retry.

Candidate recovery cannot bypass candidate integrity/dependency/risk/semantic/Home
Assistant validation, pre-deployment backup, controlled Apply, observation,
promotion or rollback safeguards. Known bad candidate SHAs and other deterministic
failures remain blocked until their work identity changes or an explicit
administrative retry is requested.

Scheduler events are sanitized: `retrigger_schedule_started`,
`retrigger_schedule_completed` and `retrigger_schedule_failed` do not include raw
options, credentials, source paths or exception text.

## Lifecycle and diagnostics

At the default log level, `service_started` reports a stable installation UUID,
unique run UUID, boot count and any interrupted previous run UUID. `service_idle`
reports the long-running service state. A SIGTERM or SIGINT sent to the container is
forwarded by the launcher to the state-owning service; the recovery schedule is
disarmed as the child service exits. `service_stopped` confirms that clean shutdown
was committed to state. An interrupted run produces a warning on the next successful
start.

| Failure reason | Action |
| --- | --- |
| `configuration_invalid` | Check supported options and their types/ranges. |
| `already_running` | Stop the other instance sharing this app's data directory. |
| `repo_b_untrusted` | Verify the configured private Repo B target, credential and pinned repository identity. |
| `state_unavailable` | Stop the app, preserve `/data/syncapp`, and inspect ownership, permissions, free space and database/schema validity. |
| `retrigger_schedule_failed` | Inspect the sanitized scheduler reason and service availability; durable work remains preserved. |
| `internal_error` | Preserve state and report the app version and sanitized event. |

Exception text and raw options are deliberately omitted from logs. Startup
failures return nonzero exit codes. Unknown or damaged state is never silently
reset. Restore a compatible app backup or return to a compatible app version;
do not delete the database as a routine repair.

The persistent `instance.lock` file is normal. The kernel releases its lock on
process exit, including forced termination or reboot. **Do not delete or replace
the lock file while any instance may be running.** Retrigger may recover interrupted
durable work, but interruption alone never authorizes bypassing deployment safety.

Container CI cannot substitute for a Home Assistant OS installation test. Before
a release, verify install/start/stop/restart/automatic reboot start, the read-only
`/homeassistant` mount, and backup/restore on a test system with protection mode
enabled.
