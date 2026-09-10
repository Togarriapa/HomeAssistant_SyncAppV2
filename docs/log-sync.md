# Guarded logs branch publication

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README defines Repo B `logs` as a Home Assistant → GitHub diagnostic branch and requires SyncApp to remain fail-closed, recoverable, and isolated from the live Home Assistant configuration tree.

`ha_syncapp.log_sync.synchronize_log_artifact()` consumes an already-staged `LogArtifact`. It first reverifies the complete artifact, captures a separate immutable snapshot, creates an isolated Git workspace, and targets only the Repo B `logs` branch.

Before any publication can be authorized, the cycle requires the repository identity already pinned in protected SyncApp state and obtains trusted evidence for the exact remote `logs` head. It uses the existing V2 publication safety mechanisms only after they were re-audited against the initial README: exact baseline anchoring, publication preflight, immutable publication intent, ordinary non-force push, post-push remote verification, and durable synchronization-baseline completion.

The cycle fails closed instead of guessing:

- an existing remote `logs` branch with no protected baseline returns `baseline_required`;
- a previously tracked branch that is now absent returns `remote_missing`;
- a remote head that differs from the protected baseline returns `diverged`;
- a byte-identical retained artifact produces `no_change` and is not pushed;
- only a proven-absent first branch or an exact known baseline can reach guarded publication.

Temporary snapshot and Git-workspace layers are deleted at the end of each cycle. The caller-owned `LogArtifact` is intentionally retained so retrigger/recovery logic can reverify and reuse the exact evidence after a transient failure.

## Deliberate exclusions

This increment does not collect live logs, alter the scheduler, process Retrigger Work Cron Job records, rewrite Git history, force-push, mutate the live Home Assistant tree, or participate in candidate deployment.

The initial README explicitly warns that deleting old log files from a Git working tree does not remove those bytes from repository history. Therefore the 30-day current artifact window and Git-history retention are separate concerns. A later milestone must design bounded `logs` history maintenance explicitly; this publication cycle must not silently rewrite history.

Candidate semantic validation remains independently blocked by issue #148. Nothing in this logs publication lane authorizes candidate backup or Apply.
