# Candidate deployment boundary

The sole product specification for candidate handling is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Current implemented boundary

The first Remote -> Home Assistant primitive is deliberately read-only. `observe_trusted_candidate()` re-proves the configured private Repo B identity through the existing pinned repository-ID trust gate and then inspects only the exact `candidate` branch.

The result is immutable evidence binding:

- Repo B target;
- stable GitHub repository ID;
- branch name `candidate`;
- the exact candidate commit SHA, or explicit trusted branch absence.

Repository, authentication, rate-limit and transport failures remain failures. They are not converted into candidate absence.

`classify_candidate()` performs only deterministic comparison with an explicitly supplied previously observed candidate SHA. Its results are `absent`, `new`, and `unchanged`. These states do not authorize deployment.

## Not implemented by this slice

Candidate detection does not fetch candidate file contents, run Git, copy remote bytes into the live Home Assistant configuration, create a backup, validate Home Assistant configuration, analyze dependencies, classify deployment risk, reload/restart Home Assistant, observe runtime health, promote to `main`, or roll back.

The next Remote -> Home Assistant increment should acquire the exact observed candidate commit into isolated protected staging, verify that the fetched commit still equals the trusted observation, and build deterministic changed-file/integrity evidence. Only after that staging boundary is independently verified should dependency analysis, risk classification and Home Assistant validation be introduced. Apply must remain downstream of validation and a recoverable pre-deployment backup, followed by observation and automatic rollback on failure.
