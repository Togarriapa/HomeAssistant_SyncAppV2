# Foundation architecture

> **Specification authority:** this document describes implementation only. The sole
> product specification is the initial `README.md` from root commit
> `71d284ce447d79b044e332c9bc01ae801dc91947`. Nothing here creates requirements
> beyond that baseline.

## Implemented boundary

Supervisor options → strict config validation → exclusive protected state store →
passive lifecycle and durable work bookkeeping. Validation happens before the
state directory is created. The app currently requests no Home Assistant
configuration mount or API privilege and performs no synchronization or deployment.

`syncapp/src/ha_syncapp/config.py` handles bounded options parsing and rejects
coercions, ambiguous JSON and unsupported settings. `state.py` owns SQLite, the
lifetime process lock, installation identity and recoverable work state.
`__main__.py` handles lifecycle and sanitized logging. Signal handlers only set a
flag; they never take Python threading locks. The idle wait checks that flag at
most every 250 ms using monotonic time.

## Protected state

`/data/syncapp` is app-owned (0700). Its regular files are 0600. The data root and
state directory must not themselves be symlinks; state/lock/SQLite sidecar files
must be regular, single-link files owned by the app's user. This assumes a trusted
Supervisor-managed data volume, not a filesystem concurrently controlled by an
attacker with the same UID or host privileges.

`instance.lock` is a stable inode with a nonblocking `flock` for the entire service
lifetime. It is never unlinked. SQLite closes before the lock descriptor closes.
SIGKILL and reboots release the OS lock, avoiding unsafe age-based lock stealing.

`state.sqlite3` uses SQLite transactions and `synchronous=FULL`. Unknown schemas,
empty/truncated databases, integrity failures, orphaned SQLite recovery files and
missing/invalid identities fail closed. Initialization is allowed only for a newly
created database. The shipped schema-1 foundation is migrated transactionally to
schema 2; the installation identity is validated before migration and is preserved.
There is no destructive reset fallback.

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
one. This detects process interruption without claiming that deployment recovery
has happened.

## Durable recoverable work

Schema 2 adds a `work` table. It intentionally stores only scheduling/lifecycle
metadata: a validated `work_kind`, deterministic `work_key`, status, attempt count
and timestamps. This primitive does not store Home Assistant configuration,
credentials or arbitrary diagnostic payloads.

A deterministic `(work_kind, work_key)` is unique. Re-enqueueing it returns the
existing state rather than creating duplicate active work. Eligible pending/retry
work is claimed transactionally and moves to `running` with an incremented attempt
counter. Work left `running` after process interruption can be returned to `retry`
when the next service instance performs recovery.

Transient failures use exponential backoff beginning at 60 seconds and capped at
one hour. After eight claimed attempts the item becomes `blocked`; permanent
failures block immediately. Blocked and successful identities are not automatically
re-executed. A changed source identity (for example, a different candidate commit)
can be represented by a different deterministic key, while an explicit future
administrative retry can be designed as a separate controlled transition.

This establishes the durable substrate required by the initial README's
**Retrigger Work Cron Job**, but does not implement the scheduler itself. Future
workers for local synchronization, pending pushes, candidate processing, runtime
collection, database snapshots, log synchronization and deployment orchestration
can use this state machine without inventing independent retry loops.

## Safety boundary still in force

No current code applies Git content to the live Home Assistant configuration. The
initial README's remote candidate model remains the required boundary: integrity
checking, dependency/risk analysis, Home Assistant validation, recoverable backup,
apply/reload, health observation, result recording, promotion on success and
rollback on failure must exist before candidate changes can be considered safe.
Git operations for local synchronization must likewise occur in staging rather
than inside the live Home Assistant configuration directory.

## Next incremental capabilities

The initial README does not require these to be implemented in a fixed order.
Each capability must receive its own tracked acceptance criteria and TDD increment.
High-value remaining prerequisites include:

1. Verify the user-supplied Repo B identity and that it is private while keeping
   SyncApp authentication credentials in protected app state.
2. Build stable local staging snapshots outside the live configuration tree with
   byte-for-byte content verification and the README-defined branch separation for
   configuration, Recorder database, runtime data and logs.
3. Connect the durable work state machine to event-driven scheduling plus the
   separate Retrigger Work Cron Job.
4. Collect normalized runtime inventory and topology information required for AI
   analysis of entities, devices, integrations, areas, services and dependencies.
5. Implement candidate integrity/diff/conflict analysis, risk classification and
   static/Home Assistant validation without live writes.
6. Add recoverable backup, guarded apply, reload/restart, observation, rollback,
   rejected-SHA blocking, known-good promotion and tagging as one tested deployment
   transaction.
7. Add consistent Recorder snapshots and bounded log retention/history management.

A working foundation or recovery primitive is not evidence that bidirectional
synchronization or controlled deployment is complete.
