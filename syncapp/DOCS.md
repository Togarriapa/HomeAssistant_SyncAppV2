# Foundation service (0.1.0)

This experimental version establishes lifecycle and storage behavior. It does
not yet connect to GitHub, synchronize configuration, create backups, restart
Home Assistant, execute recovery jobs or deploy candidates. No token is needed.

## Test installation

Until the foundation pull request is merged, copy this entire `syncapp` directory
from its development branch into `/addons/syncapp` on a **test Home Assistant OS
installation**. In Settings → Apps → App store, select Check for updates, then
install Home Assistant SyncApp V2 from Local apps. Start it manually and inspect
the Log tab. GitHub repository installation becomes available once the app files
are merged into the repository's default branch.

The app supports aarch64 (including Raspberry Pi 5) and amd64. It requests no
Home Assistant configuration mounts, API privileges, host networking or exposed
ports. The Supervisor-provided `/data` volume stores its own state. Protection
mode should remain enabled. The manifest uses cold backups so Supervisor stops
this service while backing up its persistent state.

## Options

| Option | Default | Accepted values |
| --- | --- | --- |
| `log_level` | `info` | `info`, `warning`, `error` |
| `status_interval_seconds` | `300` | Integer from 30 through 3600 |

The interval controls idle log messages, **not the Retrigger Work Cron Job**.
Unsupported keys, duplicate JSON keys and invalid values prevent startup. This
release accepts no repository URL, token or deployment-enable option.

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
| `state_unavailable` | Stop the app, preserve `/data/syncapp`, and inspect ownership, permissions, free space and database/schema validity. |
| `internal_error` | Preserve state and report the app version and sanitized event. |

Exception text and raw options are deliberately omitted from logs. Startup
failures return nonzero exit codes. Unknown or damaged state is never silently
reset. Restore a compatible app backup or return to a compatible app version;
do not delete the database as a routine repair. Recreating an empty database after
corruption would lose identity and, in later versions, recovery history.

The persistent `instance.lock` file is normal. The kernel releases its lock on
process exit, including forced termination or reboot. **Do not delete or replace
the lock file while any instance may be running.** Detection of an interrupted
service run does not authorize retrying a deployment.

Container CI cannot substitute for a Home Assistant OS installation test. Before
a release, verify install/start/stop/restart and backup/restore on a test system,
with protection mode enabled. No production deployment is part of this increment.
