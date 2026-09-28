# Automation and script load observation

The initial V2 README requires deployment observation to prove that relevant
automations and scripts load. SyncApp derives the relevant set exclusively from
the exact affected-resource target: only `automation.*` and `script.*` entity
IDs are included, so callers cannot supply unrelated resources.

This gate requires the preceding affected-entity state proof to be successful.
For a non-empty relevant set it performs one bounded, authenticated, read-only
Core WebSocket `get_states` request. Each expected entity must occur exactly
once with exact state `on` or `off`. Other states are durably classified as a
deterministic load failure. Missing, duplicate, or malformed relevant entities
also become durable deterministic load failures and are not retried unchanged.
Transport and protocol failures remain incomplete and retryable.

Production recovery accepts only the exact
`candidate_observe_automation_scripts` work identity. A stale `running` item is
returned to retry eligibility transactionally, and each Retrigger pass claims
at most one due item. A successful `loaded` proof atomically schedules
`candidate_observe_assertions`; a durable `load_failed` proof atomically
schedules `candidate_finalize`. Transient execution failures use the shared
bounded backoff, while deterministic failures are blocked from unchanged retry.
Completed proofs replay without credentials or another Core request.

Schema-v20 evidence stores prerequisite and target digests, observation time,
and expected/loaded/failed counts only. It excludes identifiers, states,
attributes, credentials, response bodies, and exception text. Empty sets and
both completed outcomes replay without credentials or network access.

The command contract follows Home Assistant Core:
<https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/websocket_api/commands.py>.

This observer grants no Apply, restart, promotion, rollback, Supervisor, or Git
mutation authority and cannot bypass any earlier deployment safeguard.
