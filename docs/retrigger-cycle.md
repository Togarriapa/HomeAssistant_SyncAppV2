# Bounded Retrigger cycle

The initial V2 README requires interrupted and retryable work to be recoverable by a periodic Retrigger mechanism. `run_retrigger_cycle()` is the scheduler-neutral execution primitive for the guarded work lanes that currently exist in V2.

One cycle runs, in deterministic order:

1. one bounded Local -> Repo B `main` recovery pass;
2. one bounded Recorder -> Repo B `database` recovery pass;
3. one bounded generated runtime -> Repo B `runtime` recovery pass.

Each lane preserves its own durable success, deterministic block, and bounded transient retry semantics. The cycle requires the live Home Assistant root, Recorder database path, isolated staging/workspace roots for every lane, Repo B target, GitHub credential, and optional Home Assistant Core API credential as explicit inputs. It does not discover paths or credentials.

The GitHub credential is used by the Repo B publication lanes. The separate Core credential is forwarded only to the runtime lane, where the independently verified runtime Retrigger pass uses it for the documented read-only Core API collection after a valid runtime work claim.

If an earlier lane cannot complete its bounded pass safely, the cycle fails closed before starting later lanes. A Local-sync failure prevents both database and runtime work. A database failure prevents runtime work. Returned errors are sanitized rather than forwarding nested exception text or credentials.

This primitive deliberately does not consume `candidate` or `logs` work. Those README-defined lanes must be added to the cycle only after their own guarded, tested transaction primitives exist. The cycle also does not schedule itself, restore database data, deploy a candidate, mutate Home Assistant, restart Home Assistant, or run Git in the live Home Assistant tree.
