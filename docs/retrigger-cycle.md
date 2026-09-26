# Bounded Retrigger cycle

The initial V2 README requires interrupted and retryable work to be recoverable by a periodic Retrigger mechanism. `run_retrigger_cycle()` is the scheduler-neutral execution primitive for the guarded work lanes that currently exist in V2.

One cycle runs, in deterministic order:

1. one bounded Local -> Repo B `main` recovery pass;
2. one bounded Recorder -> Repo B `database` recovery pass;
3. optional Recorder retention recovery;
4. one bounded generated runtime -> Repo B `runtime` recovery pass;
5. optional logs synchronization and retention recovery;
6. one bounded deployment-rollback recovery pass;
7. one bounded candidate Fetch/Stage recovery pass;
8. if Fetch/Stage performed no action, one bounded candidate integrity-analysis pass;
9. trusted candidate detection;
10. optional fresh Supervisor log collection.

Each lane preserves its own durable success, deterministic block, and bounded transient retry semantics. The cycle requires the live Home Assistant root, Recorder database path, isolated staging/workspace roots for every lane, Repo B target, GitHub credential, and optional Home Assistant Core API credential as explicit inputs. It does not discover paths or credentials.

The GitHub credential is used by Repo B publication and exact rollback-baseline proof. The separate Supervisor credential is forwarded only to guarded runtime/log collection and rollback boundaries. The rollback pass performs no credential or network access when no rollback is pending. When work exists, it accepts only the persisted exact backup and executes at most one restore, reconciliation or observation action.

The candidate Fetch/Stage pass accepts only exact `detected/fetch_stage` authority,
executes at most one action, and runs before fresh detection. Successful staging
enables `analyze` for a later cycle; it never chains into another candidate action.
The analysis pass accepts only exact `staged/analyze` authority, re-fetches the
same candidate into an isolated transient workspace, checkpoints canonical
change/integrity evidence, and enables `analyze_dependencies` for a later cycle.
Completed analysis replay is credential- and network-free. The conditional lane
ordering enforces at most one candidate action per cycle.

If an earlier lane cannot complete its bounded pass safely, the cycle fails closed before starting later lanes. A Local-sync failure prevents both database and runtime work. A database failure prevents runtime work. Returned errors are sanitized rather than forwarding nested exception text or credentials.

## Cron-invocable same-owner trigger

The service owns `StateStore` for its entire process lifetime and keeps its exclusive filesystem lock. A cron process must therefore **not** open the SQLite state independently or bypass that lock. Instead, the running service exposes a Unix-domain socket at `/data/syncapp/retrigger.sock` (relative to the configured data root). The socket lives inside the existing owner-only `0700` state directory and is itself forced to `0600`.

A cron invocation uses the explicit one-shot CLI mode:

```text
python -m ha_syncapp --retrigger-once \
  --home-assistant-root /homeassistant \
  --recorder-database /homeassistant/home-assistant_v2.db
```

The one-shot client sends only a bounded protocol version, command name, Home Assistant root and Recorder database path. It does **not** send Repo B, the GitHub credential or `SUPERVISOR_TOKEN`. The state-owning service loads and retains those credentials, re-verifies the durably bound private Repo B identity immediately before each requested cycle, and forwards `SUPERVISOR_TOKEN` only to the runtime lane.

All staging, snapshot and Git workspace roots are derived beneath the protected SyncApp data tree and are rejected if that tree overlaps the supplied Home Assistant source tree. The IPC protocol uses bounded newline-delimited JSON, rejects duplicate keys, control-character/relative paths and oversized messages, and returns only a fixed sanitized completion/failure shape.

This is the **cron-invocable primitive**, not the cron scheduler or cadence. A later packaging increment can install the periodic scheduler only after this one-shot boundary is verified on HAOS.

Candidate detection, Fetch/Stage and logs handling use their own guarded primitives;
generic `logs` work is not claimed by another lane and candidate work is claimed only
for its persisted next action. The cycle never restores Recorder data or runs Git in
the live Home Assistant tree. A Supervisor backup restore is reachable only from the
exact immutable failed-deployment authority and journal-before-mutation rollback
state machine documented in `deployment-rollback.md`.
