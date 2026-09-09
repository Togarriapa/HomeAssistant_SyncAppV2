# Bounded Retrigger cycle

The initial V2 README requires interrupted and retryable work to be recoverable by a periodic Retrigger mechanism. `run_retrigger_cycle()` is the scheduler-neutral execution primitive for the guarded work lanes that currently exist in V2.

One cycle runs, in deterministic order:

1. one bounded Local -> Repo B `main` recovery pass;
2. one bounded Recorder -> Repo B `database` recovery pass.

Each lane preserves its own durable success, deterministic block, and bounded transient retry semantics. The cycle requires the live Home Assistant root, Recorder database path, isolated staging/workspace roots, Repo B target, and authentication token as explicit inputs. It does not discover paths or credentials.

If an earlier lane cannot complete its bounded pass safely, the cycle fails closed before starting later lanes. Returned errors are sanitized rather than forwarding nested exception text or credentials.

This primitive deliberately does not consume `candidate`, `runtime`, or `logs` work. Those README-defined lanes must be added to the cycle only after their own guarded, tested transport/transaction primitives exist. The cycle also does not schedule itself, restore database data, deploy a candidate, mutate Home Assistant, restart Home Assistant, or run Git in the live Home Assistant tree.
