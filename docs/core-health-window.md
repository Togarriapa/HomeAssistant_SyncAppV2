# Configurable two-point Core health observation window

`advance_core_health_window_once()` implements the first time-based deployment
observation boundary. It binds an exact initial Core API health observation to a durable
deadline and requires a second fresh exact API-root proof at or after that deadline.

## Configuration and non-blocking progression

`deployment_observation_seconds` defaults to 300 seconds and accepts only integer values
from 30 through 3600 seconds. The chosen value is converted into an immutable deadline
when a deployment's window starts.

Each invocation performs one bounded state transition and never sleeps:

1. The first invocation persists the initial health digest, start time and deadline.
2. Invocations before the deadline return `observing` without credentials or network.
3. The first eligible invocation at or after the deadline performs at most one fresh,
   bounded authenticated Core API-root request.
4. Only the exact healthy payload can persist `completed_at` and return `healthy`.
5. A completed window replays without credentials, network access or state mutation.

An App restart or delayed Retrigger invocation therefore resumes from the durable
deadline instead of sleeping, resetting the interval or producing catch-up requests.
Transient or unhealthy final probes leave the window incomplete and may be retried by a
later controlled invocation.

## Durable integrity and authority

Schema version 14 stores one content-free window per deployment. Its deployment ID,
initial health-record digest, UTC start/deadline/completion timestamps and record digest
are revalidated on every read and transaction. A missing, corrupt, rebound, premature or
temporally impossible record fails closed.

Completion proves only that Core's authenticated API root answered at the start and end
of the configured interval. It does not prove acceptable Supervisor state, successful
integration/entity initialization, clean logs, runtime assertions, or candidate safety.
It cannot record the final deployment result, promote or tag Git, reconcile an uncertain
restart, restore a backup or authorize rollback.
