# Foundation service (0.1.0)

This experimental version establishes lifecycle, protected state, Repo B trust and
Local → GitHub synchronization safety primitives. The running service remains
passive: it does not yet execute synchronization cycles, create Home Assistant
backups, restart Home Assistant, execute recovery jobs or deploy candidates.

## Home Assistant configuration source

The app receives the supported Home Assistant App `homeassistant_config` mount at
`/homeassistant` **read-only**. This directory is the live Home Assistant
configuration source. It must never be initialized as a Git working tree and Git
commands must never run inside it.

The read-only mount is intentionally sufficient only for observing and staging
local configuration. Candidate deployment remains unimplemented in this
increment. A later delivery that needs to apply a candidate must explicitly
introduce the minimum supported write capability and must retain the initial V2
README's validation, pre-deployment backup, apply, reload/restart, observation,
verification and automatic rollback requirements. Merely changing a Git branch
must never write directly into the running Home Assistant configuration.

The app does not request Supervisor API, Home Assistant API, host networking,
host PID/IPC/DBus access, Docker API, privileged/full access, device access or
published ports. The Supervisor-provided `/data` volume stores SyncApp's own
protected state and credentials. Protection mode should remain enabled. The
manifest uses cold backups so Supervisor stops this service while backing up its
persistent state.

## Test installation

Install Home Assistant SyncApp V2 from this repository on a **test Home Assistant
OS installation**. The app supports aarch64 (including Raspberry Pi 5) and amd64.
Start it manually and inspect the Log tab. Before treating the configuration mount
as verified on hardware, confirm that `/homeassistant` is visible from the app and
that writes from the app are denied.

## Options

| Option | Default | Accepted values |
| --- | --- | --- |
| `log_level` | `info` | `info`, `warning`, `error` |
| `status_interval_seconds` | `300` | Integer from 30 through 3600 |
| `repo_b` | unset | GitHub `owner/repository` target |
| `github_token` | unset | Credential used only for trusted Repo B operations |

The interval controls idle log messages, **not the Retrigger Work Cron Job**.
Unsupported keys, duplicate JSON keys and invalid values prevent startup. Repo B
configuration remains optional while the service is passive.

## Lifecycle and diagnostics

At the default log level, `service_started` reports a stable installation UUID,
unique run UUID, boot count and any interrupted previous run UUID. `service_idle`
indicates that the foundation process is waiting; it does not report Home Assistant
health. A SIGTERM or SIGINT requests shutdown; `service_stopped` confirms the
shutdown was committed to state. An interrupted run produces a warning on the
next successful start. At `warning` or `error`, lower-severity events are hidden.

| Failure reason | Action |
| --- | --- |
| `configuration_invalid` | Check supported options and their types/ranges. |
| `already_running` | Stop the other instance sharing this app's data directory. |
| `repo_b_untrusted` | Verify the configured private Repo B target, credential and pinned repository identity. |
| `state_unavailable` | Stop the app, preserve `/data/syncapp`, and inspect ownership, permissions, free space and database/schema validity. |
| `internal_error` | Preserve state and report the app version and sanitized event. |

Exception text and raw options are deliberately omitted from logs. Startup
failures return nonzero exit codes. Unknown or damaged state is never silently
reset. Restore a compatible app backup or return to a compatible app version;
do not delete the database as a routine repair. Recreating an empty database after
corruption would lose identity and recovery history.

The persistent `instance.lock` file is normal. The kernel releases its lock on
process exit, including forced termination or reboot. **Do not delete or replace
the lock file while any instance may be running.** Detection of an interrupted
service run does not authorize retrying a deployment.

Container CI cannot substitute for a Home Assistant OS installation test. Before
a release, verify install/start/stop/restart, the read-only `/homeassistant` mount,
and backup/restore on a test system with protection mode enabled. No production
candidate deployment is part of this increment.
