# Event-driven local configuration synchronization

The sole product specification for this capability is the initial V2 root
`README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.
It requires normal Local-to-GitHub synchronization to detect stable changes and
remain separate from Retrigger recovery.

After the initial trusted Local bootstrap, a recursive Linux inotify producer
observes `/homeassistant`. It sends only a one-bit dirty signal through a bounded,
thread-safe mailbox; it never receives the `StateStore`, GitHub token, snapshot
roots, Git workspaces, file names, or file contents. The StateStore owner thread
re-observes deterministic metadata and routes only files belonging to Repo B
`main`. Recorder database and Home Assistant log churn therefore does not schedule
configuration work.

The source observer rejects a symlinked source, symlinks anywhere below it,
included special or hard-linked files, source disappearance, and root identity
changes. The inotify transport installs directory-only, no-follow watches. When a
watched directory is deleted, moved, unmounted, or ignored by the kernel, its stale
watch identity is discarded before the recursive set is refreshed. Recreating a
directory at the same path therefore cannot leave its replacement unwatched.

The owner thread compares the new metadata view with the previous safe view. A
meaningful change extends a one-second monotonic quiet-period deadline. Bursts are
coalesced, and expiration schedules one normal Local generation using the existing
deterministic work key. At most one generation is processed per service tick. A
change received during synchronization remains as one pending mailbox bit and is
re-observed and debounced on a later tick.

The processor is the existing guarded Local pipeline: private Repo B identity,
isolated byte-preserving snapshot and integrity checks, meaningful-change commit,
non-force publication authorization, remote verification, and durable baseline.
No-change completes without publication. Divergence and other deterministic
refusals are blocked; transport failures become durable bounded retry work for the
separate Retrigger path. Routine events never perform interrupted-work recovery or
administrative retry and cannot rearm blocked work.

The service starts the producer only after repository trust and normal startup
work. It re-observes after inotify readiness, closing the watch-installation race.
Shutdown stops and joins the producer before the process lock and StateStore are
released. Unexpected producer termination or unsafe source observation fails the
service closed and preserves the interrupted run for later diagnosis.

This component grants no remote Candidate Apply, backup, reload/restart,
observation, promotion, tagging, rollback, or other deployment authority. It does
not change the external Retrigger Work Cron Job.

## Verification evidence

Issue #213 is covered by deterministic source, debounce, mailbox, worker, inotify,
owner-service, durable-work, Local pipeline, and application-lifecycle tests.
They cover routed change/no-change behavior, ignored database/log churn, event
bursts, quiet-period extension, real inotify recursion, new and recreated
directories, startup race closure, meaningful publication and no-publication
results through the existing processor, change-during-sync follow-up, transient
retry handoff, transport failure, and clean or interrupted shutdown.

The exact pull-request head must pass formatting, Ruff, mypy, Bandit, full pytest,
and native amd64/aarch64 container lifecycle and semantic validation before merge.
Physical Home Assistant OS installation remains separate platform evidence.
