# Exact Supervisor health observation

The initial V2 README requires deployment observation to confirm that Supervisor
reports an acceptable state. This gate consumes only a completed, integrity-valid
Core health window and performs one bounded read-only request to

`GET http://supervisor/supervisor/info`.

The contract is pinned to Home Assistant Supervisor source commit
`64ea3be4322537fd5dcfbf620c4dc25490c1f56d`. That source registers the endpoint,
constructs its data from the authoritative `sys_core.healthy` and
`sys_core.supported` booleans, and wraps successful data in the normal Supervisor
`{"result":"ok","data":...}` envelope.

## Acceptance boundary

The request uses `SUPERVISOR_TOKEN` as a bearer credential, a finite timeout, and a
bounded response read. Success requires HTTP 200, JSON media type, no duplicate
JSON keys, exactly the top-level `result` and `data` fields, `result == "ok"`, and
the exact boolean values `data.healthy is true` and `data.supported is true`.
Missing, false, or non-boolean flags; malformed or oversized bodies; unexpected
top-level fields; transport failures; and wrong status or media type all fail
closed with sanitized errors.

Schema version 15 adds one `supervisor_health_observation` record per deployment.
It contains only the deployment identifier, the exact completed Core-window record
digest, the UTC observation time, and its own integrity digest. It deliberately
stores no response body, Supervisor/Core version, URL, header, credential, or
exception text. The observation time cannot predate completion of the bound Core
window.

The database transaction re-proves that exact binding before insert. A valid
existing record is returned before credential lookup or network access, making
completed replay idempotent, credential-free, and network-free. Corrupt, rebound,
incomplete, or missing prerequisite evidence is rejected and cannot become later
deployment authority.

## Scope

This increment proves only Supervisor health and platform support. It performs no
Home Assistant or Supervisor mutation and does not inspect integrations, entities,
logs, affected-resource assertions, or a final deployment result. It does not
authorize Repo B promotion, tagging, rollback, or backup restore. Those remain
separate ordered gates from the initial V2 README. Recurring Retrigger recovery
remains enabled and must advance this gate only through the same exact evidence and
bounded request boundary.
