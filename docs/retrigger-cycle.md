# Bounded Retrigger cycle

The initial V2 README requires interrupted and retryable work to be recoverable by a periodic Retrigger mechanism. `run_retrigger_cycle()` is the scheduler-neutral execution primitive for the guarded work lanes that currently exist in V2.

One cycle runs, in deterministic order:

1. one bounded Local -> Repo B `main` recovery pass;
2. one bounded Recorder -> Repo B `database` recovery pass;
3. one bounded generated runtime -> Repo B `runtime` recovery pass.

Each lane preserves its own durable success, deterministic block, and bounded transient retry semantics. The cycle requires the live Home Assistant root, Recorder database path, isolated staging/workspace roots for every lane, Repo B target, GitHub credential, and optional Home Assistant Core API credential as explicit inputs. It does not discover paths or credentials.

The GitHub credential is used by the Repo B publication lanes. The separate Core credential is forwarded only to the runtime lane, where the independently verified runtime Retrigger pass uses it for the documented read-only Core API collection after a valid runtime work claim.

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

This primitive deliberately does not consume `candidate` or `logs` work. Those README-defined lanes must be added to the cycle only after their own guarded, tested transaction primitives exist. The cycle also does not restore database data, deploy a candidate, mutate Home Assistant, restart Home Assistant, or run Git in the live Home Assistant tree.
