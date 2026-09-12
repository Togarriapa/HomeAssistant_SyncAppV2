# Logs branch history retention planning

This increment is derived only from the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires the generated Repo B `logs` branch to retain operational and diagnostic information for 30 days. It also explicitly notes that removing expired files from the current Git tree does not remove those bytes from older Git commits, so generated log history must not be allowed to grow without bound. Rewriting generated history is retention management; it is **not** forensic secure deletion.

## This increment

`ha_syncapp.log_history_retention` is a deterministic, side-effect-free planning boundary. Given explicit newest-first commit evidence and an explicit timezone-aware UTC reference time, it computes a fixed 30-day cutoff and classifies commit SHAs as retained or eligible for pruning.

The planner:

- accepts only the exact `logs` branch;
- requires at least one commit and always retains the supplied current head;
- retains non-head commits whose commit timestamps are exactly on or newer than the cutoff;
- classifies older commits for pruning;
- requires lowercase 40-character hexadecimal commit identities;
- rejects duplicate identities, future timestamps and history that is not newest-first;
- requires UTC timestamps and uses no hidden wall-clock state;
- bounds one planning input to 4,096 commits;
- performs no Git operations and has no network, Home Assistant, Supervisor, candidate-deployment or durable-state authority.

The timestamp supplied as `committed_at` is evidence provided by a future bounded Git-history reader. Transport integration must define and verify that evidence explicitly; this planner does not infer timestamps from repository state itself.

## Deliberately not implemented yet

This increment does **not** rewrite or force-update a remote ref. A later guarded transport increment must re-prove the pinned private Repo B repository identity, read the exact remote `logs` head, bind the plan to that head, prepare replacement history in an isolated workspace, and re-check the remote head immediately before any replacement operation. Unexpected divergence must fail closed.

History maintenance must never target `main`, `candidate`, `runtime`, or `database`. It must not alter candidate Fetch/Stage, validation, backup, Apply, reload/restart, deployment observation, promotion, tagging or rollback behavior.

The separate Retrigger Work Cron Job remains independent and unchanged.
