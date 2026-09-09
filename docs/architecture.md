# Foundation architecture

## Implemented boundary

Supervisor options → strict config validation → exclusive state store → passive
lifecycle. Validation happens before the state directory is created. The app
requests no Home Assistant filesystem or API access and makes no network calls.

`syncapp/src/ha_syncapp/config.py` handles bounded options parsing and rejects
coercions, ambiguous JSON and unsupported settings. `state.py` owns SQLite and
the lifetime process lock. `__main__.py` handles lifecycle and sanitized logging.
Signal handlers only set a flag; they never take Python threading locks. The
idle wait checks that flag at most every 250 ms using monotonic time.

## Protected state and schema 1

`/data/syncapp` is app-owned (0700). Its regular files are 0600. The data root and
state directory must not themselves be symlinks; state/lock/SQLite sidecar files
must be regular, single-link files owned by the app's user. This assumes a trusted
Supervisor-managed data volume, not a filesystem concurrently controlled by an
attacker with the same UID or host privileges.

`instance.lock` is a stable inode with a nonblocking `flock` for the entire service
lifetime. It is never unlinked. SQLite closes before the lock descriptor closes.
SIGKILL and reboots release the OS lock, avoiding unsafe age-based lock stealing.

`state.sqlite3` uses SQLite transactions, `synchronous=FULL` and schema version 1
in `PRAGMA user_version`. Creation of the schema, initial identity and schema
version is one transaction. Existing unknown versions, empty/truncated databases,
integrity failures, orphaned SQLite recovery files and missing/invalid identities
fail closed. Initialization
is only allowed for a newly created database; there is no automatic reset or
destructive migration. A failure during the first initialization may require
operator investigation instead of silently creating a second identity.

The single `installation` record contains:

| Field | Purpose |
| --- | --- |
| `singleton` | Constrained singleton key (1) |
| `installation_id` | Stable canonical UUID |
| `boot_count` | Number of successfully committed starts |
| `active_run_id` | Current UUID, cleared only on committed clean shutdown |
| `last_started_at` | UTC timestamp of the latest start |
| `last_stopped_at` | UTC timestamp of the latest clean stop |

A committed start reports the prior active UUID before replacing it with the new
one. This is process interruption detection, not a deployment recovery ledger.
Lifecycle history is bounded to the current/previous start information and stdout
events; the future logs branch and its 30-day retention are not implemented.
Cold Supervisor backups stop this app before copying state; a stopped restored
snapshot therefore has a consistent SQLite file. Actual backup/restore integration
still needs Home Assistant OS validation.

## Next increments

1. Verify Repo B's identity and privacy on authenticated GitHub reads; keep auth
   outside the synchronized configuration and never embed credentials in Git URLs.
2. Stage stable local snapshots outside live configuration; test byte fidelity,
   hidden files, concurrent writes, binary content and branch-specific exclusions.
3. Introduce versioned durable operation records, idempotency identifiers,
   bounded retry/backoff, explicit permanent failures and interrupted-work handling.
4. Implement candidate integrity, conflicts, dependency/risk checks and configuration
   validation before adding any live configuration write privilege.
5. Add confirmed recoverable backup, guarded apply, observation, rollback,
   rejected-SHA blocking and known-good promotion as one safe deployment path.
6. Add consistent Recorder snapshots, runtime/topology inventory and bounded logs.

The repository's existing full specification remains authoritative. Each increment
requires its own issue, acceptance criteria, tests and review; a working passive
service does not mean bidirectional synchronization is ready.
