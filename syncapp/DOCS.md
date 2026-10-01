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
| `repo_b_candidate_transport` | `token` | `token`, `deploy_key` |
| `repo_b_publication_transport` | `token` | `token`, `deploy_key` |
| `repo_b_promotion_transport` | `token` | `token`, `deploy_key` |
| `repo_b_rollback_transport` | `token` | `token`, `deploy_key` |
| `repo_b_retention_transport` | `token` | `token`, `deploy_key` |
| `recorder_database_path` | unset | Normalized absolute Recorder path below `/homeassistant` |
| `recorder_retention_days` | `7` | Integer from 1 through 365 |
| `administrative_retry_request_id` | unset | Canonical lowercase UUIDv4 |
| `administrative_retry_work_kind` | unset | Exact durable work kind |
| `administrative_retry_work_key` | unset | Exact durable work key; masked by Supervisor |

`status_interval_seconds` controls status logging. `retrigger_interval_seconds`
controls the separate recovery dispatcher and has no disable sentinel. If Repo B
is not configured there is no remote durable work to recover; after trusted Repo B
configuration becomes active, the dispatcher runs automatically until service
shutdown. Unsupported keys, duplicate JSON keys and invalid values prevent startup.
Recorder synchronization remains optional; omitting `recorder_database_path` skips
only the Recorder recovery lane and does not disable Local, runtime, log or candidate
recovery.

### Explicit retry of blocked work

The three `administrative_retry_*` options are an advanced, one-shot recovery
control. Configure all three together only after correcting the deterministic cause
of one known blocked item. Generate a new lowercase UUIDv4 for
`administrative_retry_request_id` and supply the item's exact work kind and key.
There is no wildcard, prefix, bulk or automatic unblock operation.

A request UUID is consumed exactly once. Its first processing either rearms the
exact blocked item with a fresh bounded retry budget or durably rejects a missing or
non-blocked target. Keeping the same options across later App restarts produces only
the sanitized `administrative_retry_skipped` event and can never rearm the item
again. To make a later deliberate attempt, use a new request UUID after verifying
the item is blocked and the underlying cause has changed.

The durable receipt stores a one-way identity digest, fixed outcome and timestamp;
it does not store the raw work kind or key. Logs contain only fixed
`administrative_retry_completed`, `administrative_retry_rejected` or
`administrative_retry_skipped` events. The option cannot bypass candidate
validation, backup, deployment observation, promotion, rollback or normal Retrigger
backoff rules.

Generated `analysis/recovery.json` includes bounded administrative retry outcome
aggregates: processed total, fixed `retried`/`rejected` counts and the latest
processing timestamp. This read-only view omits request IDs, work identities,
digests and raw options and grants no retry authority.

## Repo B deploy-key foundation

The protected Ed25519 generation boundary is implemented but generation and GitHub
enrollment are not automatic App-startup operations. It returns only the
public key, SHA-256 fingerprint, algorithm, and generation UUID; private key bytes
remain in an app-owned `0700` directory as a `0600` file. Existing generations are
validated and replayed without running `ssh-keygen` again.

Do not treat generation as proof that GitHub accepted the key. The separate
read-only access-test boundary re-proves the private Repo B numeric identity with
GitHub metadata, verifies the packaged GitHub Ed25519 host-key pin, then performs
one bounded noninteractive `git ls-remote --refs` over SSH. It returns only a
content-free observation proof and grants no transport or mutation authority.
Interrupted or tampered generations fail closed and are not automatically repaired
or rotated. See [Repo B deploy-key generation](../docs/deploy-key-generation.md)
and [Repo B deploy-key access test](../docs/deploy-key-access-test.md) for the
exact storage, identity, host-key, privacy, and recovery boundaries.

Deploy-key rotation is an explicit UUID-bound workflow: prepare a protected
candidate, manually enroll its public key while retaining the prior GitHub key,
prove read access for that exact candidate, and only then activate it locally.
Activation is journaled and recoverable across both directory-renaming crash
windows. The previous generation remains protected after activation; this does
not prove write permission or enable deploy-key transport. See
[Repo B deploy-key rotation](../docs/deploy-key-rotation.md) for lifecycle,
operator enrollment, recovery, blocking, and rollback boundaries.

Proof-bound SSH primitives exist for canonical reference reads, exact candidate
fetch, ordinary non-force publication, atomic deployment promotion plus known-good
tagging, rollback repository proof, and generated-history retention. Each
revalidates the protected generation immediately before use, passes the private
key only through an inherited descriptor, and fails closed on rebound or
conflicting evidence. Each runtime lane remains independently opt-in and defaults
to its compatibility token transport. See
[Repo B deploy-key promotion transport](../docs/deploy-key-promotion-transport.md)
for atomic reconciliation, retry and cleanup boundaries.

An explicit [Repo B initialization executor](../docs/repo-b-initialization-execution.md)
now consumes one journaled empty-repository authority, prepares a deterministic
initial `main` commit, publishes it through the descriptor-only non-force deploy-key
transport, and reconciles interruption by exact remote commit evidence. It is not
selected by routine startup, synchronization or Retrigger yet; broader deploy-key
transport adoption remains a separate reviewed increment.

Candidate ingress can now be explicitly switched to the verified deploy key with
`repo_b_candidate_transport: deploy_key`. Activation requires the trusted Repo B
identity, an existing durable `main` baseline, protected enrolled key state, and a
fresh identity-bound access proof. Periodic candidate observation and Retrigger
Fetch/Stage then use only descriptor-bound, host-pinned SSH authority and never
fall back to token Git. GitHub REST identity verification and all non-candidate
publication/deployment transports remain unchanged. See
[Deploy-key candidate ingress adoption](../docs/deploy-key-candidate-ingress-adoption.md).

Routine non-force Local, Recorder database, runtime inventory, and log artifact
publication can be independently switched with
`repo_b_publication_transport: deploy_key`. Activation requires the same pinned
identity, initialized `main` baseline, protected key generation, and fresh access
proof. Branch observation, exact baseline acquisition, and authorized publication
then use descriptor-bound, host-pinned SSH without token Git or fallback. History
retention rewrites, deployment promotion/rollback, and REST identity verification
remain independently selected. See [Deploy-key ordinary snapshot publication adoption](../docs/deploy-key-snapshot-publication-adoption.md).

Candidate promotion can be independently switched with
`repo_b_promotion_transport: deploy_key`. After the same pinned identity,
initialized `main` baseline, enrolled key, and fresh proof checks, promotion
reference observation, exact candidate fetch, atomic non-force `main`/known-good
tag publication, and reconciliation use only the proof-bound SSH authority. The
durable promotion journal and completed replay semantics are unchanged. Candidate
rollback, retention rewrites, and REST identity verification retain their existing
credentials and safeguards. See [Deploy-key candidate promotion adoption](../docs/deploy-key-promotion-adoption.md).

Rollback repository proof can be independently switched with
`repo_b_rollback_transport: deploy_key`. Activation requires the pinned private
repository identity, initialized `main` baseline, enrolled protected key, and a
fresh identity-bound access proof. Initial rollback authorization, pre-restore
re-proof, post-restore observation completion, stale-work recovery, and retry then
read the exact `refs/heads/main` value through descriptor-bound, host-pinned SSH
without token Git or fallback. Backup proof, backup restore, health checks, and
restore reconciliation remain on the Home Assistant Supervisor token. Retention
history rewrites remain independently selected. See [Deploy-key rollback repository-proof adoption](../docs/deploy-key-rollback-adoption.md).

Generated `logs` and Recorder `database` history retention can use the verified
deploy key by selecting `repo_b_retention_transport: deploy_key`. SyncApp reads the
exact branch reference, fetches and validates the complete bounded linear history,
re-proves the expected head immediately before publication, and writes only the
builder-produced replacement through an exact force-with-lease. Token and deploy-key
retention authorities cannot be mixed, and the deploy-key path has no token Git
fallback. See [Deploy-key retention history adoption](../docs/deploy-key-retention-adoption.md).

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
Assistant validation. Candidate backup execution is schema-v33 checkpointed,
journaled before mutation, and reconciled by exact request identity; uncertain
outcomes cannot trigger a blind replacement backup. Recovery cannot bypass
controlled Apply, observation,
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
| `administrative_retry_failed` | Preserve `/data/syncapp`; the request or durable receipt failed closed and no retry authority was granted. |
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
enabled. Record the Raspberry Pi 5 observation using the
[physical HAOS release evidence runbook](../docs/haos-release-evidence.md); its
checker verifies document shape and integrity, not the truth of the physical test.
